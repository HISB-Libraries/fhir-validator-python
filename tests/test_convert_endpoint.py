from tests.conftest import FakeValidatorEngine

CONVERT_URL = "/fhir/$convert"


def test_convert_json_input_defaults_to_xml_output(client, fake_engine: FakeValidatorEngine):
    fake_engine.response_body = b'<Patient xmlns="http://hl7.org/fhir"/>'
    fake_engine.response_content_type = "application/fhir+xml"

    response = client.post(
        CONVERT_URL,
        content=b'{"resourceType":"Patient"}',
        headers={"content-type": "application/fhir+json"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/fhir+xml"
    assert response.content == b'<Patient xmlns="http://hl7.org/fhir"/>'
    assert fake_engine.last_convert_call == {
        "content": b'{"resourceType":"Patient"}',
        "content_type": "application/fhir+json",
        "accept": "application/fhir+xml",
    }


def test_convert_xml_input_defaults_to_json_output(client, fake_engine: FakeValidatorEngine):
    fake_engine.response_body = b'{"resourceType":"Patient"}'
    fake_engine.response_content_type = "application/fhir+json"

    response = client.post(
        CONVERT_URL,
        content=b'<Patient xmlns="http://hl7.org/fhir"/>',
        headers={"content-type": "application/fhir+xml"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/fhir+json"
    assert fake_engine.last_convert_call == {
        "content": b'<Patient xmlns="http://hl7.org/fhir"/>',
        "content_type": "application/fhir+xml",
        "accept": "application/fhir+json",
    }


def test_convert_honors_explicit_accept_override(client, fake_engine: FakeValidatorEngine):
    # Input is JSON; without an Accept header this would default to XML, but
    # an explicit Accept should win.
    response = client.post(
        CONVERT_URL,
        content=b'{"resourceType":"Patient"}',
        headers={"content-type": "application/fhir+json", "accept": "application/fhir+json"},
    )

    assert response.status_code == 200
    assert fake_engine.last_convert_call["accept"] == "application/fhir+json"


def test_convert_defaults_json_input_when_content_type_missing(
    client, fake_engine: FakeValidatorEngine
):
    response = client.post(CONVERT_URL, content=b'{"resourceType":"Patient"}')

    assert response.status_code == 200
    assert fake_engine.last_convert_call["content_type"] == "application/fhir+json"
    assert fake_engine.last_convert_call["accept"] == "application/fhir+xml"


def test_convert_rejects_empty_body(client, fake_engine: FakeValidatorEngine):
    response = client.post(
        CONVERT_URL, content=b"", headers={"content-type": "application/fhir+json"}
    )

    assert response.status_code == 400
    # No Accept header + JSON input -> error outcome defaults to the
    # would-be target format (XML), same as a successful conversion would.
    assert response.headers["content-type"] == "application/fhir+xml"
    assert b'code value="invalid"' in response.content
    assert fake_engine.last_convert_call is None


def test_convert_returns_503_when_engine_not_running(client, fake_engine: FakeValidatorEngine):
    fake_engine.is_running = False

    response = client.post(
        CONVERT_URL,
        content=b'{"resourceType":"Patient"}',
        headers={"content-type": "application/fhir+json", "accept": "application/fhir+json"},
    )

    assert response.status_code == 503
    assert response.json()["issue"][0]["code"] == "transient"


def test_convert_returns_502_on_upstream_error(client, fake_engine: FakeValidatorEngine):
    import httpx

    fake_engine.raise_on_convert = httpx.ConnectError("connection refused")

    response = client.post(
        CONVERT_URL,
        content=b'{"resourceType":"Patient"}',
        headers={"content-type": "application/fhir+json", "accept": "application/fhir+json"},
    )

    assert response.status_code == 502
    assert response.json()["issue"][0]["code"] == "exception"
