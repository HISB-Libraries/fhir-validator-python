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
