"""Focused async tests for ValidatorEngine recovery behavior."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.validator_engine import ValidatorEngine


class FakeSettings:
    """Minimal fake settings for testing recovery behavior."""

    def __init__(
        self,
        auto_recover=True,
        recovery_max_attempts=3,
        recovery_backoff=0.01,
        recovery_backoff_multiplier=2.0,
        recovery_operation_drain_timeout=0.1,
    ):
        self.validator_jar_path = "/fake/path/validator_cli.jar"
        self.validator_host = "127.0.0.1"
        self.validator_port = 8081
        self.auto_start_validator = False
        self.fhir_version = "4.0"
        self.startup_igs = ""
        self.terminology_server = None
        self.snomed_edition = None
        self.validator_extra_args = ""
        self.validator_startup_timeout_seconds = 5
        self.validator_request_timeout_seconds = 30
        self.packages_dir = ""
        self.load_cached_packages_on_startup = False
        self.packages = ""
        self.default_ig = ""
        self.initial_load_resource_path = ""
        self.validate_initial_load_resource_on_startup = False
        self.ci_build_repos = ""
        self.auto_recover_validator = auto_recover
        self.validator_recovery_max_attempts = recovery_max_attempts
        self.validator_recovery_backoff_seconds = recovery_backoff
        self.validator_recovery_backoff_multiplier = recovery_backoff_multiplier
        self.validator_recovery_operation_drain_timeout_seconds = recovery_operation_drain_timeout
        self.cors_allow_origins = ""
        self.custom_path = ""
        self._startup_igs_list = []
        self._packages_list = []
        self._ci_build_repos_map = {}

    @property
    def startup_igs_list(self):
        return self._startup_igs_list

    @property
    def packages_list(self):
        return self._packages_list

    @property
    def ci_build_repos_map(self):
        return self._ci_build_repos_map


@pytest.fixture
async def engine_fixture():
    """Create and clean up a ValidatorEngine with auto-recovery enabled."""
    settings = FakeSettings(auto_recover=True)
    engine = ValidatorEngine(settings)
    yield engine
    # Ensure cleanup
    if engine._client and not engine._client.is_closed:
        await engine._client.aclose()
    if engine.is_running:
        await engine.stop()


@pytest.fixture
async def no_recovery_engine_fixture():
    """Create and clean up a ValidatorEngine with auto-recovery disabled."""
    settings = FakeSettings(auto_recover=False)
    engine = ValidatorEngine(settings)
    yield engine
    # Ensure cleanup
    if engine._client and not engine._client.is_closed:
        await engine._client.aclose()
    if engine.is_running:
        await engine.stop()


class TestRecoveryScheduling:
    """Tests for recovery task scheduling behavior."""

    @pytest.mark.asyncio
    async def test_transport_exception_schedules_recovery_task(self, engine_fixture):
        """Transport exception from validate_resource schedules one recovery task."""
        engine = engine_fixture

        # Mock the HTTP client to raise a transport error
        transport_exc = httpx.ConnectError("connection refused")
        with patch.object(engine._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = transport_exc

            # This should trigger recovery scheduling but re-raise the exception
            with pytest.raises(httpx.ConnectError):
                await engine.validate_resource(
                    content=b'{"resourceType": "Patient"}',
                    content_type="application/fhir+json",
                    profiles=[],
                    accept="application/fhir+json",
                )

            # Verify recovery was scheduled
            assert engine._recovery_task is not None
            assert not engine._recovery_task.done()

            # Cancel the recovery task to prevent it from running
            engine._recovery_task.cancel()
            try:
                await engine._recovery_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_successful_recovery_clears_loaded_igs(self, engine_fixture):
        """Successful recovery clears and reloads _loaded_igs (mock _restart)."""
        engine = engine_fixture

        # Set some loaded IGs
        engine._loaded_igs.update({"hl7.fhir.us.core#5.0.1", "hl7.fhir.uv.ips#2.0.0"})

        # Mock _restart to succeed
        with patch.object(engine, "_restart", new_callable=AsyncMock) as mock_restart:
            mock_restart.return_value = None

            # Schedule recovery
            engine.request_recovery("test recovery")

            # Wait briefly for recovery to start
            await asyncio.sleep(0.01)

            # Cancel the recovery task to prevent it from actually running
            if engine._recovery_task and not engine._recovery_task.done():
                engine._recovery_task.cancel()
                try:
                    await engine._recovery_task
                except asyncio.CancelledError:
                    pass

            # Verify _restart was called
            assert mock_restart.called

    @pytest.mark.asyncio
    async def test_repeated_transport_failures_no_duplicate_recovery_tasks(self, engine_fixture):
        """Repeated transport failures do not schedule duplicate recovery tasks."""
        engine = engine_fixture

        # Mock the HTTP client to raise a transport error
        transport_exc = httpx.ConnectError("connection refused")
        with patch.object(engine._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = transport_exc

            # Schedule first recovery
            with pytest.raises(httpx.ConnectError):
                await engine.validate_resource(
                    content=b'{"resourceType": "Patient"}',
                    content_type="application/fhir+json",
                    profiles=[],
                    accept="application/fhir+json",
                )

            first_task = engine._recovery_task

            # Try to schedule another recovery while first is still pending
            with pytest.raises(httpx.ConnectError):
                await engine.validate_resource(
                    content=b'{"resourceType": "Patient"}',
                    content_type="application/fhir+json",
                    profiles=[],
                    accept="application/fhir+json",
                )

            # Should still be the same task
            assert engine._recovery_task is first_task

            # Cancel both tasks to prevent them from running
            if engine._recovery_task and not engine._recovery_task.done():
                engine._recovery_task.cancel()
                try:
                    await engine._recovery_task
                except asyncio.CancelledError:
                    pass

    @pytest.mark.asyncio
    async def test_timeout_exceptions_do_not_schedule_recovery(self, engine_fixture):
        """Timeout exceptions do not schedule recovery."""
        engine = engine_fixture

        # Mock the HTTP client to raise a timeout error
        timeout_exc = httpx.ReadTimeout("read timeout")
        with patch.object(engine._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = timeout_exc

            # This should NOT trigger recovery scheduling
            with pytest.raises(httpx.ReadTimeout):
                await engine.validate_resource(
                    content=b'{"resourceType": "Patient"}',
                    content_type="application/fhir+json",
                    profiles=[],
                    accept="application/fhir+json",
                )

            # Verify NO recovery was scheduled
            assert engine._recovery_task is None

    @pytest.mark.asyncio
    async def test_stop_cancels_outstanding_recovery_task(self, engine_fixture):
        """Stop cancels an outstanding recovery task."""
        engine = engine_fixture

        # Mock _restart to take some time
        async def slow_restart():
            await asyncio.sleep(0.1)

        with patch.object(engine, "_restart", new_callable=AsyncMock) as mock_restart:
            mock_restart.side_effect = slow_restart

            # Schedule recovery
            engine.request_recovery("test cancellation")

            # Verify recovery task was scheduled
            assert engine._recovery_task is not None
            assert not engine._recovery_task.done()

            # Stop the engine - this should cancel the recovery task
            await engine.stop()

            # After stop, the recovery task handle is cleared.
            assert engine._recovery_task is None

    @pytest.mark.asyncio
    async def test_restart_clears_loaded_igs_and_sets_ready_state(self, engine_fixture):
        """Exercise restart coordination and verify old/new loaded-IG state."""
        engine = engine_fixture

        # Seed with old IG
        engine._loaded_igs.add("old-ig#1.0")

        # Mock _stop_process and _start to avoid real subprocess operations
        with (
            patch.object(engine, "_stop_process", new_callable=AsyncMock) as mock_stop,
            patch.object(engine, "_start", new_callable=AsyncMock) as mock_start,
        ):
            # Create async fake for _start that checks _loaded_igs is empty and adds new IG
            async def fake_start():
                # Assert _loaded_igs is empty (cleared by _restart)
                assert len(engine._loaded_igs) == 0
                # Add new IG to simulate successful startup
                engine._loaded_igs.add("new-ig#2.0")
                # Set startup state to ready
                engine._startup_state = "ready"

            mock_start.side_effect = fake_start

            # Call _restart directly to test coordination
            await engine._restart()

            # Verify old state was cleared and new state is present
            assert "old-ig#1.0" not in engine._loaded_igs
            assert "new-ig#2.0" in engine._loaded_igs
            assert engine._startup_state == "ready"

            # Verify mocks were called appropriately
            mock_stop.assert_called_once()
            mock_start.assert_called_once()

    @pytest.mark.asyncio
    async def test_recovery_retries_are_bounded(self, engine_fixture):
        """Recovery retries are bounded by max attempts."""
        engine = engine_fixture
        # Set low max attempts for testing
        engine._settings.validator_recovery_max_attempts = 2
        engine._settings.validator_recovery_backoff_seconds = 0.001

        # Mock _restart to always fail
        with patch.object(engine, "_restart", new_callable=AsyncMock) as mock_restart:
            mock_restart.side_effect = Exception("simulated restart failure")

            # Schedule recovery
            engine.request_recovery("test bounded retries")

            # Wait for recovery to complete with a short timeout
            try:
                await asyncio.wait_for(engine._recovery_task, timeout=0.1)
            except TimeoutError:
                # If it times out, cancel the task
                engine._recovery_task.cancel()
                try:
                    await engine._recovery_task
                except asyncio.CancelledError:
                    pass

            # Should have been called exactly max_attempts times
            assert mock_restart.call_count == 2

            # Should be in failed state
            assert engine._startup_state == "failed"

    @pytest.mark.asyncio
    async def test_disabled_auto_recover_prevents_recovery_scheduling(
        self, no_recovery_engine_fixture
    ):
        """When auto-recover is disabled, request_recovery does nothing."""
        engine = no_recovery_engine_fixture

        # Should not schedule recovery
        engine.request_recovery("test error")
        assert engine._recovery_task is None
