"""Tests for ValidatorEngine's subprocess lifecycle management.

These spawn trivial `sh`/`sleep` commands (never the real `java`/jar) to
exercise readiness detection and failure handling without requiring Java or
network access.
"""

from __future__ import annotations

import time

import pytest

from app.config import Settings
from app.validator_engine import ValidatorEngine, ValidatorEngineError

READY_LINE = "FHIR Validator HTTP Service started on port 1234"


def _engine_with_command(cmd: list[str], **settings_kwargs) -> ValidatorEngine:
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
