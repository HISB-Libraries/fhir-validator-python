"""Tests for startup preparation behavior.

These tests verify that the service correctly handles the preparation state
during startup, including:
- GET /fhir/$health returning preparation and ready states
- Requests to $validate and $convert being rejected during preparation
- FHIR OperationOutcome format for rejected requests
- XML response negotiation during preparation
"""

from tests.conftest import FakeValidatorEngine

VALIDATE_URL = "/fhir/$validate"
CONVERT_URL = "/fhir/$convert"
HEALTH_URL = "/fhir/$health"


def _params_with_inline_resource(resource: dict, igs=None, profiles=None) -> dict:
    """Helper to create a Parameters resource with an inline resource."""
    parameter = []
    for ig in igs or []:
        parameter.append({"name": "ig", "valueString": ig})
    for profile in profiles or []:
        parameter.append({"name": "profile", "valueUri": profile})
    parameter.append({"name": "resource", "resource": resource})
    return {"resourceType": "Parameters", "parameter": parameter}


def test_health_returns_preparation_status(client, fake_engine: FakeValidatorEngine):
    """GET /fhir/$health should return preparation status when engine is preparing."""
    fake_engine.is_preparing = True
    fake_engine.is_running = False
    fake_engine.loaded_igs = []

    response = client.get(HEALTH_URL)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "preparation"
    assert "Service is in preparation" in data["message"]
    assert data["running"] is False
    assert data["loaded_igs"] == []


def test_health_returns_ready_status(client, fake_engine: FakeValidatorEngine):
    """GET /fhir/$health should return ready status when engine is running."""
    fake_engine.is_preparing = False
    fake_engine.is_running = True
    fake_engine.loaded_igs = ["hl7.fhir.us.core#5.0.1"]

    response = client.get(HEALTH_URL)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert data["message"] == "Service is ready."
    assert data["running"] is True
    assert data["loaded_igs"] == ["hl7.fhir.us.core#5.0.1"]


def test_validate_rejected_during_preparation_with_json_outcome(
    client, fake_engine: FakeValidatorEngine
):
    """POST /fhir/$validate should be rejected during preparation with JSON OperationOutcome."""
    fake_engine.is_preparing = True

    response = client.post(
        VALIDATE_URL,
        json=_params_with_inline_resource({"resourceType": "Patient"}),
    )

    assert response.status_code == 503
    outcome = response.json()
    assert outcome["resourceType"] == "OperationOutcome"
    assert outcome["issue"][0]["severity"] == "error"
    assert outcome["issue"][0]["code"] == "transient"
    assert "Service is in preparation" in outcome["issue"][0]["diagnostics"]
    # Default response format should be JSON since no Accept header was sent
    assert response.headers["content-type"] == "application/fhir+json"


def test_validate_rejected_during_preparation_with_xml_outcome(
    client, fake_engine: FakeValidatorEngine
):
    """POST /fhir/$validate should be rejected during preparation with XML OperationOutcome."""
    fake_engine.is_preparing = True

    # Request with XML content type and accept header
    xml_params = (
        b'<Parameters xmlns="http://hl7.org/fhir"><parameter>'
        b'<name value="resource"/><resource><Patient xmlns="http://hl7.org/fhir"/>'
        b"</resource></parameter></Parameters>"
    )

    response = client.post(
        VALIDATE_URL,
        content=xml_params,
        headers={"content-type": "application/fhir+xml", "accept": "application/fhir+xml"},
    )

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/fhir+xml"
    assert b"<OperationOutcome" in response.content
    assert b'code value="transient"' in response.content
    assert b"Service is in preparation" in response.content


def test_convert_rejected_during_preparation_with_json_outcome(
    client, fake_engine: FakeValidatorEngine
):
    """POST /fhir/$convert should be rejected during preparation with JSON OperationOutcome."""
    fake_engine.is_preparing = True

    response = client.post(
        CONVERT_URL,
        content=b'{"resourceType": "Patient"}',
        headers={"content-type": "application/fhir+json"},
    )

    assert response.status_code == 503
    outcome = response.json()
    assert outcome["resourceType"] == "OperationOutcome"
    assert outcome["issue"][0]["severity"] == "error"
    assert outcome["issue"][0]["code"] == "transient"
    assert "Service is in preparation" in outcome["issue"][0]["diagnostics"]
    # Default response format should be JSON (target format for JSON input)
    assert response.headers["content-type"] == "application/fhir+json"


def test_convert_rejected_during_preparation_with_xml_outcome(
    client, fake_engine: FakeValidatorEngine
):
    """POST /fhir/$convert should be rejected during preparation with XML OperationOutcome."""
    fake_engine.is_preparing = True

    response = client.post(
        CONVERT_URL,
        content=b'<Patient xmlns="http://hl7.org/fhir"/>',
        headers={"content-type": "application/fhir+xml", "accept": "application/fhir+xml"},
    )

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/fhir+xml"
    assert b"<OperationOutcome" in response.content
    assert b'code value="transient"' in response.content
    assert b"Service is in preparation" in response.content
