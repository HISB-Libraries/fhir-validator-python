"""Tests for ValidatorEngine's subprocess lifecycle management.

These spawn trivial `sh`/`sleep` commands (never the real `java`/jar) to
exercise readiness detection and failure handling without requiring Java or
network access.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.validator_engine import ValidatorEngine, ValidatorEngineError

READY_LINE = "FHIR Validator HTTP Service started on port 1234"


def _engine_with_command(cmd: list[str], **settings_kwargs) -> ValidatorEngine:
    # These tests are about subprocess lifecycle, not package loading, and
    # must stay hermetic -- default to skipping the (real-cache-scanning,
    # real-HTTP-calling) startup package load, point packages_dir at a path
    # that doesn't exist, and clear packages/default_ig, so nothing here
    # ever touches this machine's real ~/.fhir/packages or the repo's own
    # packages/ folder, or makes an unexpected /loadIG call.
    settings_kwargs.setdefault("load_cached_packages_on_startup", False)
    settings_kwargs.setdefault("packages_dir", "/nonexistent-packages-dir-for-tests")
    settings_kwargs.setdefault("packages", "")
    settings_kwargs.setdefault("default_ig", "")
    settings_kwargs.setdefault("validate_initial_load_resource_on_startup", False)
    engine = ValidatorEngine(Settings(**settings_kwargs))
    engine._build_command = lambda: cmd
    return engine


async def test_start_raises_fast_when_process_exits_immediately():
    engine = _engine_with_command(
        ["sh", "-c", "echo 'Error: Unable to access jarfile bogus.jar'; exit 1"],
        validator_startup_timeout_seconds=30,
    )

    started = time.monotonic()
    with pytest.raises(ValidatorEngineError, match="exit code 1"):
        await engine.start()
    elapsed = time.monotonic() - started

    # Regression guard: must fail immediately, not wait out the full startup
    # timeout (this used to silently hang for validator_startup_timeout_seconds).
    assert elapsed < 5
    assert not engine.is_running


async def test_start_error_includes_process_output_for_diagnosis():
    engine = _engine_with_command(
        ["sh", "-c", "echo 'Unable to access jarfile /opt/validator/validator_cli.jar'; exit 1"],
        validator_startup_timeout_seconds=30,
    )

    with pytest.raises(ValidatorEngineError, match="Unable to access jarfile"):
        await engine.start()


async def test_start_times_out_when_process_runs_but_never_ready():
    engine = _engine_with_command(["sleep", "5"], validator_startup_timeout_seconds=0.3)

    started = time.monotonic()
    with pytest.raises(ValidatorEngineError, match="did not report readiness"):
        await engine.start()
    elapsed = time.monotonic() - started

    assert elapsed < 3
    assert not engine.is_running


async def test_start_succeeds_once_ready_marker_is_seen():
    engine = _engine_with_command(
        ["sh", "-c", f"echo '{READY_LINE}'; sleep 5"],
        validator_startup_timeout_seconds=10,
    )

    await engine.start()
    assert engine.is_running

    await engine.stop()
    assert not engine.is_running


# --- load_all_cached_packages() ---


def _make_package_dir(root: Path, name: str) -> None:
    (root / name / "package").mkdir(parents=True)


def _fake_post(calls: list[str], fail: set[str] | None = None, error_on: set[str] | None = None):
    fail = fail or set()
    error_on = error_on or set()

    async def post(url, json=None, **kwargs):
        ig = json["ig"]
        calls.append(ig)
        if ig in error_on:
            raise httpx.ConnectError("connection refused")
        if ig in fail:
            return httpx.Response(500, text="conflicting package already loaded")
        return httpx.Response(200)

    return post


async def test_load_all_cached_packages_loads_everything_found(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    _make_package_dir(cache_dir, "pkg.a#1.0.0")
    _make_package_dir(cache_dir, "pkg.b#2.0.0")
    monkeypatch.setattr("app.validator_engine.default_fhir_package_cache_dir", lambda: cache_dir)

    engine = ValidatorEngine(Settings())
    calls: list[str] = []
    engine._client.post = _fake_post(calls)

    await engine.load_all_cached_packages()

    assert sorted(calls) == ["pkg.a#1.0.0", "pkg.b#2.0.0"]
    assert engine._loaded_igs == {"pkg.a#1.0.0", "pkg.b#2.0.0"}


async def test_load_all_cached_packages_skips_already_loaded(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    _make_package_dir(cache_dir, "pkg.a#1.0.0")
    _make_package_dir(cache_dir, "pkg.b#2.0.0")
    monkeypatch.setattr("app.validator_engine.default_fhir_package_cache_dir", lambda: cache_dir)

    engine = ValidatorEngine(Settings())
    engine._loaded_igs.add("pkg.a#1.0.0")
    calls: list[str] = []
    engine._client.post = _fake_post(calls)

    await engine.load_all_cached_packages()

    assert calls == ["pkg.b#2.0.0"]
    assert engine._loaded_igs == {"pkg.a#1.0.0", "pkg.b#2.0.0"}


async def test_load_all_cached_packages_continues_past_http_error_responses(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    _make_package_dir(cache_dir, "pkg.a#1.0.0")
    _make_package_dir(cache_dir, "pkg.b#2.0.0")
    monkeypatch.setattr("app.validator_engine.default_fhir_package_cache_dir", lambda: cache_dir)

    engine = ValidatorEngine(Settings())
    calls: list[str] = []
    engine._client.post = _fake_post(calls, fail={"pkg.a#1.0.0"})

    # Must not raise, even though one package failed to load.
    await engine.load_all_cached_packages()

    assert sorted(calls) == ["pkg.a#1.0.0", "pkg.b#2.0.0"]
    assert engine._loaded_igs == {"pkg.b#2.0.0"}


async def test_load_all_cached_packages_continues_past_connection_errors(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    _make_package_dir(cache_dir, "pkg.a#1.0.0")
    _make_package_dir(cache_dir, "pkg.b#2.0.0")
    monkeypatch.setattr("app.validator_engine.default_fhir_package_cache_dir", lambda: cache_dir)

    engine = ValidatorEngine(Settings())
    calls: list[str] = []
    engine._client.post = _fake_post(calls, error_on={"pkg.a#1.0.0"})

    await engine.load_all_cached_packages()

    assert sorted(calls) == ["pkg.a#1.0.0", "pkg.b#2.0.0"]
    assert engine._loaded_igs == {"pkg.b#2.0.0"}


async def test_load_all_cached_packages_noop_when_cache_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.validator_engine.default_fhir_package_cache_dir", lambda: tmp_path / "empty"
    )

    engine = ValidatorEngine(Settings())
    calls: list[str] = []
    engine._client.post = _fake_post(calls)

    await engine.load_all_cached_packages()

    assert calls == []
    assert engine._loaded_igs == set()


async def test_start_loads_cached_packages_when_enabled(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    _make_package_dir(cache_dir, "pkg.a#1.0.0")
    monkeypatch.setattr("app.validator_engine.default_fhir_package_cache_dir", lambda: cache_dir)

    engine = _engine_with_command(
        ["sh", "-c", f"echo '{READY_LINE}'; sleep 5"],
        validator_startup_timeout_seconds=10,
        load_cached_packages_on_startup=True,
    )
    calls: list[str] = []
    engine._client.post = _fake_post(calls)

    await engine.start()

    assert calls == ["pkg.a#1.0.0"]
    assert engine._loaded_igs == {"pkg.a#1.0.0"}

    await engine.stop()


async def test_start_skips_cached_package_loading_when_disabled(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    _make_package_dir(cache_dir, "pkg.a#1.0.0")
    monkeypatch.setattr("app.validator_engine.default_fhir_package_cache_dir", lambda: cache_dir)

    # _engine_with_command defaults this to False already; assert explicitly.
    engine = _engine_with_command(
        ["sh", "-c", f"echo '{READY_LINE}'; sleep 5"],
        validator_startup_timeout_seconds=10,
    )
    calls: list[str] = []
    engine._client.post = _fake_post(calls)

    await engine.start()

    assert calls == []
    assert engine._loaded_igs == set()

    await engine.stop()


# --- load_configured_packages() (PACKAGES + DEFAULT_IG) ---


async def test_load_configured_packages_loads_packages_list():
    engine = ValidatorEngine(Settings(packages="pkg.a#1.0.0,pkg.b#2.0.0", default_ig=""))
    calls: list[str] = []
    engine._client.post = _fake_post(calls)

    await engine.load_configured_packages()

    assert calls == ["pkg.a#1.0.0", "pkg.b#2.0.0"]
    assert engine._loaded_igs == {"pkg.a#1.0.0", "pkg.b#2.0.0"}


async def test_load_configured_packages_loads_default_ig():
    engine = ValidatorEngine(Settings(packages="", default_ig="pkg.default#1.0.0"))
    calls: list[str] = []
    engine._client.post = _fake_post(calls)

    await engine.load_configured_packages()

    assert calls == ["pkg.default#1.0.0"]
    assert engine._loaded_igs == {"pkg.default#1.0.0"}


async def test_load_configured_packages_dedupes_default_ig_already_in_packages_list():
    engine = ValidatorEngine(
        Settings(packages="pkg.a#1.0.0,pkg.default#1.0.0", default_ig="pkg.default#1.0.0")
    )
    calls: list[str] = []
    engine._client.post = _fake_post(calls)

    await engine.load_configured_packages()

    # Loaded exactly once, not twice, despite appearing in both settings.
    assert calls == ["pkg.a#1.0.0", "pkg.default#1.0.0"]


async def test_load_configured_packages_skips_already_loaded():
    engine = ValidatorEngine(Settings(packages="pkg.a#1.0.0,pkg.b#2.0.0", default_ig=""))
    engine._loaded_igs.add("pkg.a#1.0.0")
    calls: list[str] = []
    engine._client.post = _fake_post(calls)

    await engine.load_configured_packages()

    assert calls == ["pkg.b#2.0.0"]


async def test_load_configured_packages_continues_past_failures():
    engine = ValidatorEngine(Settings(packages="pkg.a#1.0.0,pkg.b#2.0.0", default_ig=""))
    calls: list[str] = []
    engine._client.post = _fake_post(calls, fail={"pkg.a#1.0.0"})

    # Must not raise, even though one package failed to load.
    await engine.load_configured_packages()

    assert sorted(calls) == ["pkg.a#1.0.0", "pkg.b#2.0.0"]
    assert engine._loaded_igs == {"pkg.b#2.0.0"}


async def test_load_configured_packages_noop_when_unset():
    engine = ValidatorEngine(Settings(packages="", default_ig=""))
    calls: list[str] = []
    engine._client.post = _fake_post(calls)

    await engine.load_configured_packages()

    assert calls == []


async def test_start_loads_configured_packages(monkeypatch):
    engine = _engine_with_command(
        ["sh", "-c", f"echo '{READY_LINE}'; sleep 5"],
        validator_startup_timeout_seconds=10,
        packages="pkg.a#1.0.0",
        default_ig="pkg.default#1.0.0",
    )
    calls: list[str] = []
    engine._client.post = _fake_post(calls)

    await engine.start()

    assert sorted(calls) == ["pkg.a#1.0.0", "pkg.default#1.0.0"]
    assert engine._loaded_igs == {"pkg.a#1.0.0", "pkg.default#1.0.0"}

    await engine.stop()


# --- load_configured_packages() fallback chain: registry -> build.fhir.org
# -> local packages_dir (see app/ci_build.py, app/package_cache.py) ---


async def test_load_configured_package_falls_back_to_ci_build_download(tmp_path, monkeypatch):
    downloaded: list[tuple] = []

    async def fake_download(org_repo, package_id, version, cache_dir, **kwargs):
        downloaded.append((org_repo, package_id, version))
        return True

    monkeypatch.setattr("app.validator_engine.download_ci_build_package", fake_download)
    monkeypatch.setattr(
        "app.validator_engine.default_fhir_package_cache_dir", lambda: tmp_path / "cache"
    )

    engine = ValidatorEngine(
        Settings(
            packages="hl7.fhir.us.vdor#0.1.1-cibuild",
            default_ig="",
            ci_build_repos="hl7.fhir.us.vdor#0.1.1-cibuild=HL7/fhir-vdor",
            packages_dir="/nonexistent-packages-dir-for-tests",
        )
    )
    calls: list[str] = []
    # First /loadIG attempt fails (not on the registry); second (after the
    # simulated ci-build download) succeeds.
    call_count = {"n": 0}

    async def post(url, json=None, **kwargs):
        calls.append(json["ig"])
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(500, text="not found")
        return httpx.Response(200)

    engine._client.post = post

    await engine.load_configured_packages()

    assert downloaded == [("HL7/fhir-vdor", "hl7.fhir.us.vdor", "0.1.1-cibuild")]
    assert calls == ["hl7.fhir.us.vdor#0.1.1-cibuild", "hl7.fhir.us.vdor#0.1.1-cibuild"]
    assert engine._loaded_igs == {"hl7.fhir.us.vdor#0.1.1-cibuild"}


async def test_load_configured_package_skips_ci_build_download_without_mapping(
    tmp_path, monkeypatch
):
    downloaded: list[tuple] = []

    async def fake_download(org_repo, package_id, version, cache_dir, **kwargs):
        downloaded.append((org_repo, package_id, version))
        return True

    monkeypatch.setattr("app.validator_engine.download_ci_build_package", fake_download)
    monkeypatch.setattr(
        "app.validator_engine.default_fhir_package_cache_dir", lambda: tmp_path / "cache"
    )

    # No CI_BUILD_REPOS entry for this package -- should go straight to the
    # local packages_dir tier without attempting a build.fhir.org download.
    engine = ValidatorEngine(
        Settings(
            packages="hl7.fhir.us.vdor#0.1.1-cibuild",
            default_ig="",
            ci_build_repos="",
            packages_dir="/nonexistent-packages-dir-for-tests",
        )
    )
    calls: list[str] = []
    engine._client.post = _fake_post(calls, fail={"hl7.fhir.us.vdor#0.1.1-cibuild"})

    await engine.load_configured_packages()

    assert downloaded == []
    assert engine._loaded_igs == set()


async def test_load_configured_package_skips_ci_build_download_for_published_version(
    tmp_path, monkeypatch
):
    downloaded: list[tuple] = []

    async def fake_download(org_repo, package_id, version, cache_dir, **kwargs):
        downloaded.append((org_repo, package_id, version))
        return True

    monkeypatch.setattr("app.validator_engine.download_ci_build_package", fake_download)
    monkeypatch.setattr(
        "app.validator_engine.default_fhir_package_cache_dir", lambda: tmp_path / "cache"
    )

    # Mapping present, but the version doesn't look like a draft/ci-build
    # (e.g. a "ballot" package, which is routinely published) -- must not
    # attempt the build.fhir.org download.
    engine = ValidatorEngine(
        Settings(
            packages="hl7.fhir.us.bser#2.0.0-ballot",
            default_ig="",
            ci_build_repos="hl7.fhir.us.bser#2.0.0-ballot=HL7/fhir-bser",
            packages_dir="/nonexistent-packages-dir-for-tests",
        )
    )
    calls: list[str] = []
    engine._client.post = _fake_post(calls, fail={"hl7.fhir.us.bser#2.0.0-ballot"})

    await engine.load_configured_packages()

    assert downloaded == []
    assert engine._loaded_igs == set()


async def test_load_configured_package_falls_back_to_local_packages_dir(tmp_path, monkeypatch):
    packages_dir = tmp_path / "packages"
    cache_dir = tmp_path / "cache"
    _make_package_dir(packages_dir, "hl7.fhir.us.mdi#3.0.0-draft")
    monkeypatch.setattr("app.validator_engine.default_fhir_package_cache_dir", lambda: cache_dir)

    engine = ValidatorEngine(
        Settings(
            packages="hl7.fhir.us.mdi#3.0.0-draft",
            default_ig="",
            ci_build_repos="",  # no build.fhir.org mapping -- straight to local
            packages_dir=str(packages_dir),
        )
    )
    calls: list[str] = []
    engine._client.post = _fake_post(calls, fail={"hl7.fhir.us.mdi#3.0.0-draft"})

    await engine.load_configured_packages()

    # First attempt failed (simulated registry 500); after the local
    # packages_dir copy, a second /loadIG attempt is made and (per
    # _fake_post's unconditional "fail" set) also fails here -- so assert
    # the copy happened even though the mock keeps failing every call.
    assert (cache_dir / "hl7.fhir.us.mdi#3.0.0-draft" / "package").is_dir()
    assert calls == ["hl7.fhir.us.mdi#3.0.0-draft", "hl7.fhir.us.mdi#3.0.0-draft"]


async def test_load_configured_package_local_fallback_succeeds(tmp_path, monkeypatch):
    packages_dir = tmp_path / "packages"
    cache_dir = tmp_path / "cache"
    _make_package_dir(packages_dir, "hl7.fhir.us.mdi#3.0.0-draft")
    monkeypatch.setattr("app.validator_engine.default_fhir_package_cache_dir", lambda: cache_dir)

    engine = ValidatorEngine(
        Settings(
            packages="hl7.fhir.us.mdi#3.0.0-draft",
            default_ig="",
            ci_build_repos="",
            packages_dir=str(packages_dir),
        )
    )
    calls: list[str] = []
    call_count = {"n": 0}

    async def post(url, json=None, **kwargs):
        calls.append(json["ig"])
        call_count["n"] += 1
        # Fails until it's actually available in the cache (i.e. only
        # succeeds on the retry after the local packages_dir copy).
        if (cache_dir / json["ig"] / "package").is_dir():
            return httpx.Response(200)
        return httpx.Response(500, text="not found")

    engine._client.post = post

    await engine.load_configured_packages()

    assert calls == ["hl7.fhir.us.mdi#3.0.0-draft", "hl7.fhir.us.mdi#3.0.0-draft"]
    assert engine._loaded_igs == {"hl7.fhir.us.mdi#3.0.0-draft"}


async def test_load_configured_package_fails_when_all_tiers_exhausted(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.validator_engine.default_fhir_package_cache_dir", lambda: tmp_path / "cache"
    )

    engine = ValidatorEngine(
        Settings(
            packages="hl7.fhir.us.bogus#9.9.9-draft",
            default_ig="",
            ci_build_repos="",
            packages_dir="/nonexistent-packages-dir-for-tests",
        )
    )
    calls: list[str] = []
    engine._client.post = _fake_post(calls, fail={"hl7.fhir.us.bogus#9.9.9-draft"})

    # Must not raise, even though every tier failed.
    await engine.load_configured_packages()

    assert engine._loaded_igs == set()


# --- start(): STARTUP_IGS local preloading, no more blanket packages_dir
# preload for PACKAGES/DEFAULT_IG ---


async def test_start_preloads_only_startup_igs_from_packages_dir(tmp_path, monkeypatch):
    packages_dir = tmp_path / "packages"
    cache_dir = tmp_path / "cache"
    _make_package_dir(packages_dir, "hl7.fhir.us.startup-local#1.0.0-cibuild")
    _make_package_dir(packages_dir, "hl7.fhir.us.not-referenced#1.0.0-cibuild")
    monkeypatch.setattr("app.validator_engine.default_fhir_package_cache_dir", lambda: cache_dir)

    engine = _engine_with_command(
        ["sh", "-c", f"echo '{READY_LINE}'; sleep 5"],
        validator_startup_timeout_seconds=10,
        packages_dir=str(packages_dir),
        startup_igs="hl7.fhir.us.startup-local#1.0.0-cibuild",
    )
    calls: list[str] = []
    engine._client.post = _fake_post(calls)

    await engine.start()

    # Only the package actually referenced by STARTUP_IGS is preloaded --
    # the other packages_dir entry is left alone (it's not a blanket copy
    # anymore).
    assert (cache_dir / "hl7.fhir.us.startup-local#1.0.0-cibuild" / "package").is_dir()
    assert not (cache_dir / "hl7.fhir.us.not-referenced#1.0.0-cibuild").exists()

    await engine.stop()


# --- validate_initial_load_resource() (startup warm-up) ---


def _make_resource_file(path: Path, content: str = '{"resourceType": "Patient"}') -> None:
    path.write_text(content)


async def test_validate_initial_load_resource_noop_when_default_ig_empty(tmp_path):
    resource_path = tmp_path / "initial_load_resource.json"
    _make_resource_file(resource_path)

    engine = ValidatorEngine(Settings(default_ig="", initial_load_resource_path=str(resource_path)))
    calls: list[dict] = []

    async def fake_validate_resource(**kwargs):
        calls.append(kwargs)
        raise AssertionError("should not be called")

    engine.validate_resource = fake_validate_resource

    await engine.validate_initial_load_resource()

    assert calls == []


async def test_validate_initial_load_resource_noop_when_disabled(tmp_path):
    resource_path = tmp_path / "initial_load_resource.json"
    _make_resource_file(resource_path)

    engine = ValidatorEngine(
        Settings(
            default_ig="pkg.default#1.0.0",
            initial_load_resource_path=str(resource_path),
            validate_initial_load_resource_on_startup=False,
        )
    )

    async def fake_validate_resource(**kwargs):
        raise AssertionError("should not be called")

    engine.validate_resource = fake_validate_resource

    await engine.validate_initial_load_resource()


async def test_validate_initial_load_resource_noop_when_file_missing(tmp_path):
    engine = ValidatorEngine(
        Settings(
            default_ig="pkg.default#1.0.0",
            initial_load_resource_path=str(tmp_path / "does-not-exist.json"),
        )
    )

    async def fake_validate_resource(**kwargs):
        raise AssertionError("should not be called")

    engine.validate_resource = fake_validate_resource

    await engine.validate_initial_load_resource()


async def test_validate_initial_load_resource_validates_json_file(tmp_path):
    resource_path = tmp_path / "initial_load_resource.json"
    _make_resource_file(resource_path, '{"resourceType": "Patient", "id": "abc"}')

    engine = ValidatorEngine(
        Settings(default_ig="pkg.default#1.0.0", initial_load_resource_path=str(resource_path))
    )
    calls: list[dict] = []

    async def fake_validate_resource(**kwargs):
        calls.append(kwargs)
        return httpx.Response(
            200,
            content=b'{"resourceType":"OperationOutcome","issue":[]}',
            headers={"content-type": "application/fhir+json"},
        )

    engine.validate_resource = fake_validate_resource

    await engine.validate_initial_load_resource()

    assert len(calls) == 1
    call = calls[0]
    assert call["content"] == resource_path.read_bytes()
    assert call["content_type"] == "application/fhir+json"
    assert call["profiles"] == []


async def test_validate_initial_load_resource_detects_xml_by_extension(tmp_path):
    resource_path = tmp_path / "initial_load_resource.xml"
    _make_resource_file(resource_path, '<Patient xmlns="http://hl7.org/fhir"></Patient>')

    engine = ValidatorEngine(
        Settings(default_ig="pkg.default#1.0.0", initial_load_resource_path=str(resource_path))
    )
    calls: list[dict] = []

    async def fake_validate_resource(**kwargs):
        calls.append(kwargs)
        return httpx.Response(
            200,
            content=b'{"resourceType":"OperationOutcome","issue":[]}',
            headers={"content-type": "application/fhir+json"},
        )

    engine.validate_resource = fake_validate_resource

    await engine.validate_initial_load_resource()

    assert calls[0]["content_type"] == "application/fhir+xml"


async def test_validate_initial_load_resource_does_not_raise_on_error_response(tmp_path):
    resource_path = tmp_path / "initial_load_resource.json"
    _make_resource_file(resource_path)

    engine = ValidatorEngine(
        Settings(default_ig="pkg.default#1.0.0", initial_load_resource_path=str(resource_path))
    )

    async def fake_validate_resource(**kwargs):
        return httpx.Response(
            500,
            content=b'{"resourceType":"OperationOutcome","issue":[{"severity":"error"}]}',
            headers={"content-type": "application/fhir+json"},
        )

    engine.validate_resource = fake_validate_resource

    # Must not raise, even though the upstream response is an error.
    await engine.validate_initial_load_resource()


async def test_validate_initial_load_resource_does_not_raise_on_connection_error(tmp_path):
    resource_path = tmp_path / "initial_load_resource.json"
    _make_resource_file(resource_path)

    engine = ValidatorEngine(
        Settings(default_ig="pkg.default#1.0.0", initial_load_resource_path=str(resource_path))
    )

    async def fake_validate_resource(**kwargs):
        raise httpx.ConnectError("connection refused")

    engine.validate_resource = fake_validate_resource

    # Must not raise, even though the engine is unreachable.
    await engine.validate_initial_load_resource()


async def test_start_calls_validate_initial_load_resource(tmp_path):
    resource_path = tmp_path / "initial_load_resource.json"
    _make_resource_file(resource_path)

    engine = _engine_with_command(
        ["sh", "-c", f"echo '{READY_LINE}'; sleep 5"],
        validator_startup_timeout_seconds=10,
        default_ig="pkg.default#1.0.0",
        initial_load_resource_path=str(resource_path),
        validate_initial_load_resource_on_startup=True,
    )
    calls: list[str] = []
    load_ig_post = _fake_post(calls)

    async def post(url, json=None, **kwargs):
        if url == "/loadIG":
            return await load_ig_post(url, json=json, **kwargs)
        return httpx.Response(
            200,
            content=b'{"resourceType":"OperationOutcome","issue":[]}',
            headers={"content-type": "application/fhir+json"},
        )

    engine._client.post = post

    validated: list[dict] = []
    original = engine.validate_resource

    async def spy_validate_resource(**kwargs):
        validated.append(kwargs)
        return await original(**kwargs)

    engine.validate_resource = spy_validate_resource

    await engine.start()

    assert len(validated) == 1
    assert calls == ["pkg.default#1.0.0"]

    await engine.stop()
