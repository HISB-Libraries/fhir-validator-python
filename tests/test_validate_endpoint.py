import json

from tests.conftest import FakeValidatorEngine

VALIDATE_URL = "/fhir/$validate"


def _params_with_inline_resource(resource: dict, igs=None, profiles=None) -> dict:
    parameter = []
    for ig in igs or []:
        parameter.append({"name": "ig", "valueString": ig})
    for profile in profiles or []:
        parameter.append({"name": "profile", "valueUri": profile})
    parameter.append({"name": "resource", "resource": resource})
    return {"resourceType": "Parameters", "parameter": parameter}


def test_validate_happy_path_returns_upstream_operation_outcome(
    client, fake_engine: FakeValidatorEngine
):
    fake_engine.response_body = json.dumps(
        {
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "information", "code": "informational"}],
        }
    ).encode()

    body = _params_with_inline_resource(
        {"resourceType": "Patient", "name": [{"family": "Doe"}]},
        igs=["hl7.fhir.us.core#5.0.1"],
        profiles=["http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient"],
    )

    response = client.post(VALIDATE_URL, json=body)

    assert response.status_code == 200
    assert response.json()["resourceType"] == "OperationOutcome"
    assert fake_engine.loaded_igs == ["hl7.fhir.us.core#5.0.1"]
    assert fake_engine.last_validate_call["profiles"] == [
        "http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient"
    ]
    assert fake_engine.last_validate_call["content_type"] == "application/fhir+json"


def test_validate_rejects_non_parameters_body(client):
    response = client.post(VALIDATE_URL, json={"resourceType": "Patient"})

    assert response.status_code == 400
    outcome = response.json()
    assert outcome["resourceType"] == "OperationOutcome"
    assert outcome["issue"][0]["code"] == "invalid"


def test_validate_rejects_missing_resource_parameter(client):
    response = client.post(
        VALIDATE_URL,
        json={
            "resourceType": "Parameters",
            "parameter": [{"name": "ig", "valueString": "hl7.fhir.uv.ips"}],
        },
    )

    assert response.status_code == 400
    assert "resource" in response.json()["issue"][0]["diagnostics"]


def test_validate_returns_503_when_engine_not_running(client, fake_engine: FakeValidatorEngine):
    fake_engine.is_running = False

    response = client.post(
        VALIDATE_URL, json=_params_with_inline_resource({"resourceType": "Patient"})
    )

    assert response.status_code == 503
    assert response.json()["issue"][0]["code"] == "transient"


def test_validate_returns_502_on_upstream_error(client, fake_engine: FakeValidatorEngine):
    import httpx

    fake_engine.raise_on_validate = httpx.ConnectError("connection refused")

    response = client.post(
        VALIDATE_URL, json=_params_with_inline_resource({"resourceType": "Patient"})
    )

    assert response.status_code == 502
    assert response.json()["issue"][0]["code"] == "exception"


def test_healthz_reports_engine_status(client, fake_engine: FakeValidatorEngine):
    fake_engine.loaded_igs = ["hl7.fhir.uv.ips"]

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"running": True, "loaded_igs": ["hl7.fhir.uv.ips"]}


# --- XML request body support ---

_XML_PARAMETERS_WITH_INLINE_RESOURCE = b"""<Parameters xmlns="http://hl7.org/fhir">
  <parameter>
    <name value="ig"/>
    <valueString value="hl7.fhir.us.core#5.0.1"/>
  </parameter>
  <parameter>
    <name value="resource"/>
    <resource>
      <Patient xmlns="http://hl7.org/fhir"><name><family value="Doe"/></name></Patient>
    </resource>
  </parameter>
</Parameters>"""


def test_validate_accepts_xml_request_body(client, fake_engine: FakeValidatorEngine):
    fake_engine.response_body = (
        b'<OperationOutcome xmlns="http://hl7.org/fhir"><issue>'
        b'<severity value="information"/><code value="informational"/>'
        b"</issue></OperationOutcome>"
    )
    fake_engine.response_content_type = "application/fhir+xml"

    response = client.post(
        VALIDATE_URL,
        content=_XML_PARAMETERS_WITH_INLINE_RESOURCE,
        headers={"content-type": "application/fhir+xml"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/fhir+xml"
    assert b"<OperationOutcome" in response.content
    assert fake_engine.loaded_igs == ["hl7.fhir.us.core#5.0.1"]
    assert fake_engine.last_validate_call["content_type"] == "application/fhir+xml"
    # No Accept header was sent, so the response format defaults to the
    # request envelope's own representation (XML).
    assert fake_engine.last_validate_call["accept"] == "application/fhir+xml"


def test_validate_honors_accept_header_override_for_xml_request(
    client, fake_engine: FakeValidatorEngine
):
    response = client.post(
        VALIDATE_URL,
        content=_XML_PARAMETERS_WITH_INLINE_RESOURCE,
        headers={"content-type": "application/fhir+xml", "accept": "application/fhir+json"},
    )

    assert response.status_code == 200
    assert fake_engine.last_validate_call["accept"] == "application/fhir+json"


def test_validate_rejects_malformed_xml_with_xml_outcome(client):
    response = client.post(
        VALIDATE_URL,
        content=b"<Parameters><oops",
        headers={"content-type": "application/fhir+xml"},
    )

    assert response.status_code == 400
    assert response.headers["content-type"] == "application/fhir+xml"
    assert b'code value="invalid"' in response.content
    assert b"not well-formed XML" in response.content


def test_validate_rejects_missing_resource_parameter_xml(client):
    response = client.post(
        VALIDATE_URL,
        content=b'<Parameters xmlns="http://hl7.org/fhir">'
        b'<parameter><name value="ig"/><valueString value="hl7.fhir.uv.ips"/></parameter>'
        b"</Parameters>",
        headers={"content-type": "application/fhir+xml"},
    )

    assert response.status_code == 400
    assert b"resource" in response.content
