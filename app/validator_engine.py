"""Manage the persistent HL7 FHIR Validator CLI process.

The validator jar supports a `server` subcommand that loads a validation
engine once (an expensive, minutes-long operation the first time IGs need
to be downloaded) and then serves HTTP requests against it indefinitely.
This module starts that subprocess, waits for it to report readiness, and
proxies validation/IG-load calls to it over HTTP so the FastAPI layer never
pays the engine construction cost per-request.

Reference: "Running the Validator as a local HTTP service"
https://confluence.hl7.org/spaces/FHIR/pages/441520076/
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from app.ci_build import download_ci_build_package, is_ci_build_version
from app.config import Settings
from app.package_cache import (
    default_fhir_package_cache_dir,
    list_cached_packages,
    preload_package,
)

logger = logging.getLogger("fhir_validator.engine")

READY_MARKER = "FHIR Validator HTTP Service started"


class ValidatorEngineError(RuntimeError):
    """Raised when the validator engine process fails to start or respond."""


class ValidatorEngine:
    """Owns the `java -jar validator_cli.jar server <port>` subprocess."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._process: asyncio.subprocess.Process | None = None
        self._log_task: asyncio.Task[None] | None = None
        self._startup_log: list[str] = []
        self._loaded_igs: set[str] = set()
        self._ig_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            base_url=f"http://{settings.validator_host}:{settings.validator_port}",
            timeout=settings.validator_request_timeout_seconds,
        )

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def _build_command(self) -> list[str]:
        cmd = [
            "java",
            "-jar",
            self._settings.validator_jar_path,
            "server",
            str(self._settings.validator_port),
            "-version",
            self._settings.fhir_version,
        ]
        for ig in self._settings.startup_igs_list:
            cmd += ["-ig", ig]
        if self._settings.terminology_server:
            cmd += ["-tx", self._settings.terminology_server]
        cmd += self._settings.validator_extra_args_list
        return cmd

    async def start(self) -> None:
        """Launch the validator subprocess and block until it is ready.

        Fails fast (rather than waiting out the full startup timeout) if the
        process exits before reporting readiness -- e.g. a bad
        `VALIDATOR_JAR_PATH`, missing Java, or invalid CLI args -- and
        includes the process's own output (which has the real error, such
        as "Unable to access jarfile ...") in the raised exception.
        """
        if self.is_running:
            return

        # STARTUP_IGS are passed straight to the validator jar as `-ig` args
        # below (see _build_command) -- the jar resolves those itself
        # (cache-first, network-fallback) the instant it starts, with no
        # chance for us to intervene afterwards. So any of them that are
        # local-only IGs (not published on the FHIR package registry) must
        # already be on disk *before* we spawn it. This is unrelated to
        # `packages`/`default_ig` below, which we load ourselves via
        # `/loadIG` once the engine is up, and which apply the full
        # registry -> build.fhir.org -> packages_dir fallback chain (see
        # `load_configured_packages`) instead of unconditionally preloading.
        packages_dir = Path(self._settings.packages_dir)
        cache_dir = default_fhir_package_cache_dir()
        for ig in self._settings.startup_igs_list:
            if await asyncio.to_thread(preload_package, ig, packages_dir, cache_dir):
                logger.info("Preloaded startup IG %s into the FHIR cache", ig)

        cmd = self._build_command()
        logger.info("Starting validator engine: %s", " ".join(cmd))
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._startup_log = []

        ready = asyncio.Event()
        self._log_task = asyncio.create_task(self._pump_logs(ready))

        ready_task = asyncio.create_task(ready.wait())
        exit_task = asyncio.create_task(self._process.wait())
        try:
            done, pending = await asyncio.wait(
                {ready_task, exit_task},
                timeout=self._settings.validator_startup_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (ready_task, exit_task):
                if not task.done():
                    task.cancel()

        if ready_task in done:
            pass  # engine reported readiness
        elif exit_task in done:
            returncode = exit_task.result()
            # Give the log pump a brief moment to drain any output that was
            # still buffered when the process exited.
            if self._log_task is not None:
                try:
                    await asyncio.wait_for(self._log_task, timeout=2)
                except (TimeoutError, asyncio.CancelledError):
                    pass
            log_tail = "\n".join(self._startup_log[-20:]) or "(no output captured)"
            await self.stop()
            raise ValidatorEngineError(
                f"Validator engine process exited during startup (exit code {returncode}). "
                f"Last output:\n{log_tail}"
            )
        else:
            await self.stop()
            raise ValidatorEngineError(
                "Validator engine did not report readiness within "
                f"{self._settings.validator_startup_timeout_seconds}s"
            )

        if self._settings.startup_igs_list:
            self._loaded_igs.update(self._settings.startup_igs_list)

        await self.load_configured_packages()

        if self._settings.load_cached_packages_on_startup:
            await self.load_all_cached_packages()

        await self.validate_initial_load_resource()

    async def _pump_logs(self, ready: asyncio.Event) -> None:
        """Continuously drain the subprocess's stdout (avoids pipe deadlock),
        flip `ready` once the server announces it's listening, and keep a
        rolling buffer of recent lines for diagnosing startup failures."""
        assert self._process is not None
        assert self._process.stdout is not None
        while True:
            line = await self._process.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip()
            logger.info("[validator] %s", text)
            self._startup_log.append(text)
            del self._startup_log[:-200]
            if READY_MARKER in text:
                ready.set()

    async def stop(self) -> None:
        if self._log_task is not None:
            self._log_task.cancel()
            self._log_task = None
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=10)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()
        self._process = None
        await self._client.aclose()

    async def ensure_igs_loaded(self, igs: list[str]) -> None:
        """Load any IGs not already loaded into the running engine via
        POST /loadIG, so requests never need to restart the engine."""
        missing = [ig for ig in igs if ig not in self._loaded_igs]
        if not missing:
            return
        async with self._ig_lock:
            for ig in missing:
                if ig in self._loaded_igs:
                    continue
                response = await self._client.post("/loadIG", json={"ig": ig})
                if response.status_code >= 400:
                    raise ValidatorEngineError(
                        f"Failed to load IG '{ig}': {response.status_code} {response.text}"
                    )
                self._loaded_igs.add(ig)

    async def load_configured_packages(self) -> None:
        """Ensure every package listed in `PACKAGES` (see `GET /fhir/$packages`)
        plus `DEFAULT_IG` is loaded into the running engine.

        Each package is tried against, in order, up to three tiers (see
        `_load_configured_package`): the FHIR package registry, then (for
        draft/ci-build-looking versions with a `CI_BUILD_REPOS` entry) a
        direct download from build.fhir.org, then the local `packages_dir`
        folder. Best-effort: a package that fails all three tiers is logged
        and skipped, same as `load_all_cached_packages()` below.
        """
        targets: list[str] = []
        for pkg in [*self._settings.packages_list, self._settings.default_ig]:
            if pkg and pkg not in targets:
                targets.append(pkg)

        to_load = [pkg for pkg in targets if pkg not in self._loaded_igs]
        if not to_load:
            return

        logger.info("Loading %d configured package(s) into the engine: %s", len(to_load), to_load)

        loaded: list[str] = []
        failed: list[str] = []
        async with self._ig_lock:
            for pkg in to_load:
                if pkg in self._loaded_igs:
                    continue
                if await self._load_configured_package(pkg):
                    loaded.append(pkg)
                else:
                    failed.append(pkg)

        logger.info(
            "Loaded %d/%d configured package(s) into the engine%s",
            len(loaded),
            len(to_load),
            f" (failed: {failed})" if failed else "",
        )

    async def _try_load_ig(self, pkg: str) -> bool:
        """POST /loadIG for a single package, returning whether it
        succeeded (never raises)."""
        try:
            response = await self._client.post("/loadIG", json={"ig": pkg})
        except httpx.HTTPError as exc:
            logger.debug("loadIG failed for %s: %s", pkg, exc)
            return False
        if response.status_code >= 400:
            logger.debug("loadIG failed for %s: %s %s", pkg, response.status_code, response.text)
            return False
        return True

    async def _load_configured_package(self, pkg: str) -> bool:
        """Load a single `packages`/`default_ig` entry into the engine,
        trying up to three tiers in order and stopping at the first that
        succeeds:

        1. The FHIR package registry, via `/loadIG` (cache-first,
           network-fallback -- inherent validator behavior).
        2. For versions that look like drafts/ci-builds (see
           `is_ci_build_version`) with a `CI_BUILD_REPOS` entry, a direct
           download from build.fhir.org (see `app/ci_build.py`), then a
           retry of `/loadIG`.
        3. The local `packages_dir` folder (see `app/package_cache.py`),
           then a final retry of `/loadIG`.

        Returns whether the package ended up loaded; marks it in
        `_loaded_igs` on success. Caller (`load_configured_packages`) holds
        `_ig_lock` for the duration.
        """
        if await self._try_load_ig(pkg):
            self._loaded_igs.add(pkg)
            return True

        package_id, _, version = pkg.partition("#")
        cache_dir = default_fhir_package_cache_dir()

        org_repo = self._settings.ci_build_repos_map.get(pkg)
        if org_repo and is_ci_build_version(version):
            logger.info(
                "Package %s not available from the FHIR package registry; "
                "trying build.fhir.org (%s)",
                pkg,
                org_repo,
            )
            if await download_ci_build_package(org_repo, package_id, version, cache_dir):
                if await self._try_load_ig(pkg):
                    self._loaded_igs.add(pkg)
                    return True

        preload_package(pkg, Path(self._settings.packages_dir), cache_dir)
        if (cache_dir / pkg / "package").is_dir() and await self._try_load_ig(pkg):
            self._loaded_igs.add(pkg)
            return True

        logger.warning("Failed to load configured package %s from any source", pkg)
        return False

    async def load_all_cached_packages(self) -> None:
        """Load every package already present in the FHIR package cache
        (`~/.fhir/packages`) into the running engine via POST /loadIG, not
        just the ones referenced by a request's "ig" parameter or passed via
        STARTUP_IGS/PACKAGES/DEFAULT_IG.

        Best-effort and non-fatal by design: the cache is a shared,
        long-lived directory that can accumulate packages from unrelated
        prior activity (different FHIR versions, multiple versions of the
        same IG) which may legitimately fail to load together in a single
        engine. A failure loading one package is logged and skipped -- it
        does not raise, abort startup, or block the rest from loading.
        """
        cache_dir = default_fhir_package_cache_dir()
        cached = list_cached_packages(cache_dir)
        to_load = [pkg for pkg in cached if pkg not in self._loaded_igs]
        if not to_load:
            return

        logger.info("Loading %d cached package(s) into the engine: %s", len(to_load), to_load)
        await self._load_igs_best_effort(to_load)

    async def _load_igs_best_effort(self, igs: list[str]) -> None:
        """Shared by load_configured_packages()/load_all_cached_packages():
        POST /loadIG for each of `igs`, logging and skipping (never raising)
        any that fail."""
        loaded: list[str] = []
        failed: list[str] = []
        async with self._ig_lock:
            for pkg in igs:
                if pkg in self._loaded_igs:
                    continue
                try:
                    response = await self._client.post("/loadIG", json={"ig": pkg})
                except httpx.HTTPError as exc:
                    failed.append(pkg)
                    logger.warning("Failed to load package %s: %s", pkg, exc)
                    continue
                if response.status_code >= 400:
                    failed.append(pkg)
                    logger.warning(
                        "Failed to load package %s: %s %s", pkg, response.status_code, response.text
                    )
                    continue
                self._loaded_igs.add(pkg)
                loaded.append(pkg)

        logger.info(
            "Loaded %d/%d package(s) into the engine%s",
            len(loaded),
            len(igs),
            f" (failed: {failed})" if failed else "",
        )

    async def validate_initial_load_resource(self) -> None:
        """Best-effort startup warm-up: validate `initial_load_resource_path`
        (if configured, present, and `default_ig` is set) against the engine
        now that `default_ig` and its dependencies have finished loading.

        This exercises the full validate path once at startup -- surfacing
        any IG/profile mismatch between the configured `default_ig` and the
        sample resource in the logs immediately, rather than waiting for the
        first real request to discover it. Purely diagnostic: it never
        raises or blocks startup, regardless of the validation outcome.
        """
        if not self._settings.validate_initial_load_resource_on_startup:
            return
        if not self._settings.default_ig:
            return

        path = Path(self._settings.initial_load_resource_path)
        if not path.is_file():
            logger.debug("Initial load resource %s not found; skipping startup validation", path)
            return

        content = path.read_bytes()
        content_type = (
            "application/fhir+xml" if path.suffix.lower() == ".xml" else "application/fhir+json"
        )

        logger.info(
            "Validating initial load resource %s against DEFAULT_IG %s",
            path,
            self._settings.default_ig,
        )
        try:
            response = await self.validate_resource(
                content=content,
                content_type=content_type,
                profiles=[],
                accept="application/fhir+json",
            )
        except httpx.HTTPError as exc:
            logger.warning("Startup validation of %s could not reach the engine: %s", path, exc)
            return

        issue_count: int | str = "?"
        try:
            outcome = json.loads(response.content)
            issue_count = len(outcome.get("issue", []))
        except ValueError:
            pass

        if response.status_code >= 400:
            logger.warning(
                "Startup validation of %s returned HTTP %s (%s issue(s)): %s",
                path,
                response.status_code,
                issue_count,
                response.text[:2000],
            )
        else:
            logger.info(
                "Startup validation of %s completed successfully (%s issue(s))",
                path,
                issue_count,
            )

    async def validate_resource(
        self,
        content: bytes,
        content_type: str,
        profiles: list[str],
        accept: str,
    ) -> httpx.Response:
        params: list[tuple[str, str]] = [("profile", p) for p in profiles]
        headers = {"Content-Type": content_type, "Accept": accept}
        return await self._client.post(
            "/validateResource", params=params, content=content, headers=headers
        )

    async def convert_resource(
        self,
        content: bytes,
        content_type: str,
        accept: str,
    ) -> httpx.Response:
        """Proxy to the engine's `POST /convert` (JSON<->XML format
        conversion). Note the engine itself defaults to JSON output when
        `Accept` is omitted -- it does *not* infer "the other format" from
        `content_type` -- so callers wanting a JSON<->XML flip must always
        pass an explicit `accept`."""
        headers = {"Content-Type": content_type, "Accept": accept}
        return await self._client.post("/convert", content=content, headers=headers)

    async def health(self) -> dict[str, Any]:
        return {
            "running": self.is_running,
            "loaded_igs": sorted(self._loaded_igs),
        }
