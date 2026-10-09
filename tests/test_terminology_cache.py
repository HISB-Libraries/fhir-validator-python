"""Focused regression tests for terminology cache fix.

Tests cover:
- Settings.terminology_cache_dir_path uses a service-specific default and honors an explicit path
- _build_command includes -txCache and optionally -clear-tx-cache
- A stale cache response schedules recovery with clear_tx_cache
- Ordinary validator HTTP 4xx/5xx and transport timeout do not schedule cache recovery
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.config import Settings
from app.validator_engine import ValidatorEngine


class FakeSettings:
    """Minimal fake settings for testing terminology cache behavior."""

    def __init__(
        self,
        terminology_cache_dir="",
        clear_terminology_cache_on_startup=False,
        auto_recover=True,
        terminology_server=None,
        snomed_edition="us",
        **kwargs,
    ):
        # Common defaults from unit tests
        self.validator_jar_path = "/fake/path/validator_cli.jar"
        self.validator_host = "127.0.0.1"
        self.validator_port = 8081
        self.auto_start_validator = False
        self.fhir_version = "4.0"
        self.startup_igs = ""
        self.packages_dir = ""
        self.load_cached_packages_on_startup = False
        self.packages = ""
        self.default_ig = ""
        self.initial_load_resource_path = ""
        self.validate_initial_load_resource_on_startup = False
        self.ci_build_repos = ""
        self.validator_startup_timeout_seconds = 5
        self.validator_request_timeout_seconds = 30
        self.validator_extra_args = ""
        self.cors_allow_origins = ""
        self.custom_path = ""
        # Configure specific fields for this test
        self.terminology_cache_dir = terminology_cache_dir
        self.clear_terminology_cache_on_startup = clear_terminology_cache_on_startup
        self.auto_recover_validator = auto_recover
        self.terminology_server = terminology_server
        self.snomed_edition = snomed_edition
        # Recovery settings needed for tests
        self.validator_recovery_max_attempts = 3
        self.validator_recovery_backoff_seconds = 5.0
        self.validator_recovery_backoff_multiplier = 2.0
        self.validator_recovery_operation_drain_timeout_seconds = 10.0
        # Fake properties that must be implemented
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

    @property
    def terminology_cache_dir_path(self):
        # This mimics the real implementation in Settings
        configured = self.terminology_cache_dir.strip()
        if configured:
            from pathlib import Path

            return str(Path(configured).expanduser())
        # Default path as defined in Settings
        from pathlib import Path

        return str(Path.home() / ".fhir" / "validator-service" / "terminology-cache")

    @property
    def validator_extra_args_list(self):
        return []


class TestTerminologyCacheDirPath:
    """Tests for Settings.terminology_cache_dir_path property."""

    def test_uses_service_specific_default_when_not_configured(self):
        """When terminology_cache_dir is empty, use a service-specific default."""
        settings = Settings(terminology_cache_dir="")
        path = settings.terminology_cache_dir_path

        # Should use the service-specific default path
        expected_path = str(Path.home() / ".fhir" / "validator-service" / "terminology-cache")
        assert path == expected_path
        assert "validator-service" in path

    def test_honors_explicit_path(self):
        """When terminology_cache_dir is set, use that path."""
        settings = Settings(terminology_cache_dir="/custom/cache/path")
        path = settings.terminology_cache_dir_path

        # Should use the explicitly configured path
        assert path == "/custom/cache/path"

    def test_expands_user_in_configured_path(self):
        """When terminology_cache_dir contains ~, expand it."""
        settings = Settings(terminology_cache_dir="~/my/cache")
        path = settings.terminology_cache_dir_path

        # Should expand the ~
        home = str(Path.home())
        assert path == f"{home}/my/cache"


class TestBuildCommand:
    """Tests for _build_command() and terminology cache flags."""

    def test_includes_txCache_with_default_path(self):
        """_build_command includes -txCache with the default terminology cache path."""
        settings = FakeSettings()
        engine = ValidatorEngine(settings)

        cmd = engine._build_command()

        # Should include -txCache with the default path
        assert "-txCache" in cmd
        tx_cache_index = cmd.index("-txCache")
        assert cmd[tx_cache_index + 1] == settings.terminology_cache_dir_path

    def test_includes_txCache_with_configured_path(self):
        """_build_command includes -txCache with the configured terminology cache path."""
        settings = FakeSettings(terminology_cache_dir="/custom/cache/path")
        engine = ValidatorEngine(settings)

        cmd = engine._build_command()

        # Should include -txCache with the configured path
        assert "-txCache" in cmd
        tx_cache_index = cmd.index("-txCache")
        assert cmd[tx_cache_index + 1] == "/custom/cache/path"

    def test_includes_clear_tx_cache_when_requested(self):
        """_build_command includes -clear-tx-cache when configured."""
        settings = FakeSettings(clear_terminology_cache_on_startup=True)
        engine = ValidatorEngine(settings)

        cmd = engine._build_command()

        # Should include -clear-tx-cache flag
        assert "-clear-tx-cache" in cmd

    def test_excludes_clear_tx_cache_when_not_requested(self):
        """_build_command excludes -clear-tx-cache when not configured."""
        settings = FakeSettings(clear_terminology_cache_on_startup=False)
        engine = ValidatorEngine(settings)

        cmd = engine._build_command()

        # Should not include -clear-tx-cache flag
        assert "-clear-tx-cache" not in cmd

    def test_clears_clear_tx_cache_flag_after_build(self):
        """_build_command consumes the clear-cache flag."""
        settings = FakeSettings(clear_terminology_cache_on_startup=True)
        engine = ValidatorEngine(settings)

        # Initial state should have the flag set
        assert engine._clear_tx_cache_next_start is True

        cmd = engine._build_command()

        # Should include -clear-tx-cache flag
        assert "-clear-tx-cache" in cmd

        # Flag should be cleared after build
        assert engine._clear_tx_cache_next_start is False

    def test_restores_clear_tx_cache_flag_if_set_again(self):
        """If _clear_tx_cache_next_start is set again after being cleared, it's honored."""
        settings = FakeSettings(clear_terminology_cache_on_startup=True)
        engine = ValidatorEngine(settings)

        # Build command once (clears the flag)
        cmd = engine._build_command()
        assert engine._clear_tx_cache_next_start is False

        # Set flag again
        engine._clear_tx_cache_next_start = True

        # Build command again - should include -clear-tx-cache again
        cmd = engine._build_command()
        assert "-clear-tx-cache" in cmd
        assert engine._clear_tx_cache_next_start is False


class TestStaleCacheResponseRecovery:
    """Tests for recovery scheduling when stale terminology cache responses are detected."""

    @pytest.fixture
    def engine_with_recovery(self):
        """Create a ValidatorEngine with auto-recovery enabled."""
        settings = FakeSettings(auto_recover=True)
        engine = ValidatorEngine(settings)
        yield engine
        # Simple cleanup - just cancel any recovery task
        if engine._recovery_task and not engine._recovery_task.done():
            engine._recovery_task.cancel()
            try:
                engine._recovery_task.result()
            except (asyncio.CancelledError, Exception):
                pass

    @pytest.fixture
    def no_recovery_engine(self):
        """Create a ValidatorEngine with auto-recovery disabled."""
        settings = FakeSettings(auto_recover=False)
        engine = ValidatorEngine(settings)
        yield engine
        # Simple cleanup - just cancel any recovery task
        if engine._recovery_task and not engine._recovery_task.done():
            engine._recovery_task.cancel()
            try:
                engine._recovery_task.result()
            except (asyncio.CancelledError, Exception):
                pass

    @pytest.mark.asyncio
    async def test_stale_cache_response_schedules_recovery_with_clear_tx_cache(
        self, engine_with_recovery
    ):
        """A stale cache response schedules recovery with clear_tx_cache."""
        engine = engine_with_recovery

        # Response with both stale cache markers
        response = httpx.Response(
            500,
            content=b"<html>never issued by this servercache-control?mode=start</html>",
        )

        # Schedule recovery for this response
        engine._request_recovery_for_response(response, "/validateResource")

        # Verify recovery was scheduled immediately (before it starts clearing flags)
        assert engine._recovery_task is not None

        # The _clear_tx_cache_next_start should be True right after scheduling
        # (it gets cleared when the recovery process actually starts)
        assert engine._clear_tx_cache_next_start is True

        # Cancel the recovery to prevent it from running further
        if engine._recovery_task and not engine._recovery_task.done():
            engine._recovery_task.cancel()
            try:
                await engine._recovery_task
            except asyncio.CancelledError:
                pass

    def test_non_stale_error_does_not_schedule_cache_recovery(self, engine_with_recovery):
        """Ordinary validator HTTP error responses do not schedule cache recovery."""
        engine = engine_with_recovery

        # Regular error response without stale cache markers
        response = httpx.Response(
            500,
            content=b'{"resourceType":"OperationOutcome","issue":[{"severity":"error","code":"invalid"}]}',
        )

        # Schedule recovery for this response
        engine._request_recovery_for_response(response, "/validateResource")

        # Recovery should NOT be scheduled for cache clearing
        assert engine._recovery_task is None
        assert engine._clear_tx_cache_next_start is False

    def test_explanatory_text_without_stale_cache_marker_does_not_trigger_recovery(
        self, engine_with_recovery
    ):
        """Cache-control text without the server-issued marker does not trigger recovery."""
        engine = engine_with_recovery

        response = httpx.Response(500, content=b"cache-control?mode=start")

        engine._request_recovery_for_response(response, "/validateResource")

        assert engine._recovery_task is None
        assert engine._clear_tx_cache_next_start is False

    def test_successful_response_does_not_schedule_recovery(self, engine_with_recovery):
        """Successful HTTP responses do not trigger recovery."""
        engine = engine_with_recovery

        # Successful response
        response = httpx.Response(200, content=b'{"resourceType":"OperationOutcome","issue":[]}')

        # Schedule recovery for this response
        engine._request_recovery_for_response(response, "/validateResource")

        # Recovery should NOT be scheduled
        assert engine._recovery_task is None
        assert engine._clear_tx_cache_next_start is False

    def test_disabled_auto_recover_prevents_recovery(self, no_recovery_engine):
        """Disabled auto-recovery prevents scheduling for stale cache responses."""
        engine = no_recovery_engine

        # Response with both stale cache markers
        response = httpx.Response(
            500, content=b"<html>never issued by this servercache-control?mode=start</html>"
        )

        # Schedule recovery for this response
        engine._request_recovery_for_response(response, "/validateResource")

        # Recovery should NOT be scheduled due to disabled auto-recover
        assert engine._recovery_task is None
        assert engine._clear_tx_cache_next_start is False

    @pytest.mark.asyncio
    async def test_transport_timeout_does_not_schedule_cache_recovery(self, engine_with_recovery):
        """Transport timeout exceptions do not schedule cache recovery."""
        engine = engine_with_recovery

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
            assert engine._clear_tx_cache_next_start is False

    def test_ordinary_http_4xx_does_not_schedule_cache_recovery(self, engine_with_recovery):
        """Ordinary HTTP 4xx responses do not schedule cache recovery."""
        engine = engine_with_recovery

        # HTTP 400 response without stale cache markers
        response = httpx.Response(
            400,
            content=b'{"resourceType":"OperationOutcome","issue":[{"severity":"error","code":"invalid"}]}',
        )

        # Schedule recovery for this response
        engine._request_recovery_for_response(response, "/validateResource")

        # Recovery should NOT be scheduled
        assert engine._recovery_task is None
        assert engine._clear_tx_cache_next_start is False

    def test_ordinary_http_5xx_does_not_schedule_cache_recovery(self, engine_with_recovery):
        """Ordinary HTTP 5xx responses do not schedule cache recovery."""
        engine = engine_with_recovery

        # HTTP 500 response without stale cache markers
        response = httpx.Response(
            500,
            content=b'{"resourceType":"OperationOutcome","issue":[{"severity":"fatal","code":"exception"}]}',
        )

        # Schedule recovery for this response
        engine._request_recovery_for_response(response, "/validateResource")

        # Recovery should NOT be scheduled
        assert engine._recovery_task is None
        assert engine._clear_tx_cache_next_start is False

    @pytest.mark.asyncio
    async def test_http_200_with_expired_cache_diagnostic_schedules_recovery(
        self, engine_with_recovery
    ):
        """A 200 response with an expired-cache UUID schedules recovery."""
        engine = engine_with_recovery

        # Response with expired cache diagnostic containing UUID
        response = httpx.Response(
            200,
            content=(
                b'{"resourceType":"OperationOutcome","issue":[{"severity":"warning",'
                b'"code":"invalid","diagnostics":"The Coding provided was not found; '
                b"error message = Error from https://tx.fhir.org/r4: Error: The cache "
                b"'3d1b2c4e-5f6a-7b8c-9d0e-1f2a3b4c5d6e' expired: it went 34 minutes"
                b"}]}"
            ),
        )

        # Schedule recovery for this response
        engine._request_recovery_for_response(response, "/validateResource")

        # Recovery should be scheduled with clear_tx_cache flag
        assert engine._recovery_task is not None
        assert engine._clear_tx_cache_next_start is True

        # Cancel the recovery to prevent it from running further
        if engine._recovery_task and not engine._recovery_task.done():
            engine._recovery_task.cancel()
            try:
                await engine._recovery_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_http_500_with_expired_cache_diagnostic_schedules_recovery(
        self, engine_with_recovery
    ):
        """A HTTP 500 response containing the expired-cache diagnostic also schedules recovery."""
        engine = engine_with_recovery

        # Response with expired cache diagnostic containing UUID in 500 response
        response = httpx.Response(
            500,
            content=(
                b'{"resourceType":"OperationOutcome","issue":[{"severity":"error",'
                b'"code":"expired","diagnostics":"the cache 12345678-1234-1234-1234-'
                b'123456789abc expired:"}]}'
            ),
        )

        # Schedule recovery for this response
        engine._request_recovery_for_response(response, "/validateResource")

        # Recovery should be scheduled with clear_tx_cache flag
        assert engine._recovery_task is not None
        assert engine._clear_tx_cache_next_start is True

        # Cancel the recovery to prevent it from running further
        if engine._recovery_task and not engine._recovery_task.done():
            engine._recovery_task.cancel()
            try:
                await engine._recovery_task
            except asyncio.CancelledError:
                pass

    def test_http_200_without_cache_session_diagnostic_does_not_schedule_recovery(
        self, engine_with_recovery
    ):
        """An ordinary 200 coding error does not schedule cache recovery."""
        engine = engine_with_recovery

        # Response with ordinary coding/value-set error but no cache-session diagnostic
        response = httpx.Response(
            200,
            content=(
                b'{"resourceType":"OperationOutcome","issue":[{"severity":"error",'
                b'"code":"invalid","diagnostics":"Cannot find code system"}]}'
            ),
        )

        # Schedule recovery for this response
        engine._request_recovery_for_response(response, "/validateResource")

        # Recovery should NOT be scheduled
        assert engine._recovery_task is None
        assert engine._clear_tx_cache_next_start is False

    def test_generic_expired_text_without_uuid_does_not_schedule_recovery(
        self, engine_with_recovery
    ):
        """Generic expired text without a cache UUID does not schedule recovery."""
        engine = engine_with_recovery

        # Response with generic expired text without UUID
        response = httpx.Response(
            200,
            content=(
                b'{"resourceType":"OperationOutcome","issue":[{"severity":"warning",'
                b'"code":"expired","diagnostics":"subscription expired at midnight"}]}'
            ),
        )

        # Schedule recovery for this response
        engine._request_recovery_for_response(response, "/validateResource")

        # Recovery should NOT be scheduled
        assert engine._recovery_task is None
        assert engine._clear_tx_cache_next_start is False
