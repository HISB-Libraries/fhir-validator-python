"""Fetch draft/ci-build FHIR IG packages directly from build.fhir.org.

Packages that are still "in development" (draft/ci-build/screenshot
versions) are often not published to the FHIR package registry
(packages.fhir.org) yet, but their latest build is always available at a
predictable URL on HL7's CI build server:

    https://build.fhir.org/ig/<Org-or-User>/<Repo-Name>/package.tgz

This is the second of three tiers tried, in order, for every package in
`PACKAGES`/`DEFAULT_IG` (see `ValidatorEngine.load_configured_packages`):
  1. The FHIR package registry, via the validator's own `/loadIG` (which
     resolves cache-first/network-fallback -- inherent validator behavior).
  2. This module -- only for versions that look like drafts/ci-builds (see
     `is_ci_build_version`), and only if the package has an entry in
     `CI_BUILD_REPOS` (see `Settings.ci_build_repos_map`). We deliberately
     don't try to guess a GitHub org/repo from the package id -- it's not
     derivable in general (e.g. `hl7.fhir.us.mdi` builds from
     `HL7/fhir-mdi-ig`, not `HL7/fhir-us-mdi` or `HL7/mdi`).
  3. The repo's local `packages/` folder (see `app/package_cache.py`).
"""

from __future__ import annotations

import io
import logging
import shutil
import tarfile
from pathlib import Path

import httpx

logger = logging.getLogger("fhir_validator.ci_build")

CI_BUILD_BASE_URL = "https://build.fhir.org/ig"

# Deliberately excludes "ballot" -- ballot versions *are* routinely
# published to the FHIR package registry (verified for
# hl7.fhir.us.bser#2.0.0-ballot, see AGENTS.md), so they should go through
# tier 1 (the registry) like any other published version, not this tier.
_CI_BUILD_VERSION_MARKERS = ("draft", "cibuild", "ci-build", "screenshot")


def is_ci_build_version(version: str) -> bool:
    """True if `version` looks like a draft/ci-build/screenshot version --
    i.e. one that's plausibly not yet published to the FHIR package
    registry, per HL7's own versioning conventions."""
    lowered = version.lower()
    return any(marker in lowered for marker in _CI_BUILD_VERSION_MARKERS)


async def download_ci_build_package(
    org_repo: str,
    package_id: str,
    version: str,
    cache_dir: Path,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 120.0,
) -> bool:
    """Download `<CI_BUILD_BASE_URL>/<org_repo>/package.tgz` and extract it
    into `cache_dir` as `<package_id>#<version>`. Returns True on success,
    False on any failure (network error, non-200 response, malformed/
    unexpected archive shape) -- this is a best-effort fallback tier that
    never raises.

    `client` is exposed purely for tests (inject an `httpx.AsyncClient`
    backed by a `MockTransport`); callers in production code should leave
    it unset, which spins up (and cleans up) a short-lived client just for
    this one download."""
    url = f"{CI_BUILD_BASE_URL}/{org_repo}/package.tgz"
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    try:
        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:
            logger.warning("Failed to download ci-build package from %s: %s", url, exc)
            return False
    finally:
        if owns_client:
            await client.aclose()

    if response.status_code >= 400:
        logger.warning("Failed to download ci-build package from %s: %s", url, response.status_code)
        return False

    return _extract_package_tgz(response.content, package_id, version, cache_dir, url)


def _extract_package_tgz(
    content: bytes, package_id: str, version: str, cache_dir: Path, source_url: str
) -> bool:
    name = f"{package_id}#{version}"
    destination = cache_dir / name
    tmp_destination = cache_dir / f".preloading-{name}"

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        if tmp_destination.exists():
            shutil.rmtree(tmp_destination)
        tmp_destination.mkdir(parents=True)

        extract_kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tar:
            tar.extractall(tmp_destination, **extract_kwargs)

        if not (tmp_destination / "package").is_dir():
            logger.warning(
                "ci-build package.tgz from %s did not contain a package/ folder", source_url
            )
            shutil.rmtree(tmp_destination, ignore_errors=True)
            return False

        if destination.exists():
            shutil.rmtree(destination)
        tmp_destination.replace(destination)
    except (OSError, tarfile.TarError) as exc:
        logger.warning("Failed to extract ci-build package from %s: %s", source_url, exc)
        shutil.rmtree(tmp_destination, ignore_errors=True)
        return False

    logger.info("Downloaded ci-build package %s from %s", name, source_url)
    return True
