"""Shared test fixtures.

Tests never spawn the real Java validator process; instead a
`FakeValidatorEngine` is swapped onto `app.state.validator_engine` after the
app's lifespan startup runs (with `AUTO_START_VALIDATOR=false` so no real
subprocess is attempted).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@dataclass
class FakeValidatorEngine:
    is_running: bool = True
    loaded_igs: list[str] = field(default_factory=list)
    response_status: int = 200
    response_body: bytes = b'{"resourceType":"OperationOutcome","issue":[]}'
    response_content_type: str = "application/fhir+json"
    last_validate_call: dict | None = None
    raise_on_validate: Exception | None = None

    async def ensure_igs_loaded(self, igs: list[str]) -> None:
        for ig in igs:
            if ig not in self.loaded_igs:
                self.loaded_igs.append(ig)

    async def validate_resource(self, content, content_type, profiles, accept):
        if self.raise_on_validate:
            raise self.raise_on_validate
        self.last_validate_call = {
            "content": content,
            "content_type": content_type,
            "profiles": profiles,
            "accept": accept,
        }
        return httpx.Response(
            status_code=self.response_status,
            content=self.response_body,
            headers={"content-type": self.response_content_type},
        )

    async def health(self) -> dict:
        return {"running": self.is_running, "loaded_igs": self.loaded_igs}


@pytest.fixture
def fake_engine() -> FakeValidatorEngine:
    return FakeValidatorEngine()


@pytest.fixture
def client(fake_engine: FakeValidatorEngine):
    app = create_app(Settings(auto_start_validator=False))
    with TestClient(app) as test_client:
        app.state.validator_engine = fake_engine
        yield test_client
