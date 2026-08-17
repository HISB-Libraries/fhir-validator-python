import base64

import pytest

from app.fhir_parameters import ParametersError
from app.fhir_xml import operation_outcome_to_xml, parse_validate_parameters_xml

FHIR_NS = "http://hl7.org/fhir"


def _wrap(parameters_xml: str) -> bytes:
    return f'<Parameters xmlns="{FHIR_NS}">{parameters_xml}</Parameters>'.encode()


def test_parses_inline_resource_and_repeating_ig_and_profile():
    body = _wrap(
        """
        <parameter><name value="ig"/><valueString value="hl7.fhir.us.core#5.0.1"/></parameter>
        <parameter><name value="ig"/><valueString value="hl7.fhir.uv.ips"/></parameter>
        <parameter><name value="profile"/>
          <valueUri value="http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient"/>
        </parameter>
        <parameter><name value="resource"/>
          <resource>
            <Patient xmlns="http://hl7.org/fhir"><name><family value="Doe"/></name></Patient>
          </resource>
        </parameter>
        """
    )

    result = parse_validate_parameters_xml(body, default_format="application/fhir+json")

    assert result.igs == ["hl7.fhir.us.core#5.0.1", "hl7.fhir.uv.ips"]
    assert result.profiles == ["http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient"]
    # Inline resources are always forwarded as XML regardless of declared format.
    assert result.format == "application/fhir+xml"
    assert b"<Patient" in result.resource_content
    assert b'<family value="Doe"' in result.resource_content


def test_parses_raw_string_resource_with_declared_json_format():
    body = _wrap(
        """
        <parameter><name value="format"/><valueCode value="application/json"/></parameter>
        <parameter><name value="resource"/>
          <valueString value="{&quot;resourceType&quot;:&quot;Patient&quot;}"/>
        </parameter>
        """
    )

    result = parse_validate_parameters_xml(body, default_format="application/fhir+xml")

    assert result.format == "application/fhir+json"
    assert result.resource_content == b'{"resourceType":"Patient"}'


def test_parses_base64_binary_resource():
    raw = b"<Patient xmlns='http://hl7.org/fhir'/>"
    body = _wrap(
        f'<parameter><name value="resource"/>'
        f'<valueBase64Binary value="{base64.b64encode(raw).decode()}"/></parameter>'
    )

    result = parse_validate_parameters_xml(body, default_format="application/fhir+xml")

    assert result.resource_content == raw
    assert result.format == "application/fhir+xml"


def test_defaults_format_when_not_declared():
    body = _wrap('<parameter><name value="resource"/><valueString value="x"/></parameter>')

    result = parse_validate_parameters_xml(body, default_format="application/fhir+xml")

    assert result.format == "application/fhir+xml"


def test_rejects_non_parameters_root():
    body = b'<Patient xmlns="http://hl7.org/fhir"/>'
    with pytest.raises(ParametersError, match="Parameters"):
        parse_validate_parameters_xml(body, default_format="application/fhir+json")


def test_rejects_missing_resource_parameter():
    body = _wrap('<parameter><name value="ig"/><valueString value="hl7.fhir.uv.ips"/></parameter>')
    with pytest.raises(ParametersError, match="resource"):
        parse_validate_parameters_xml(body, default_format="application/fhir+json")


def test_rejects_malformed_xml():
    with pytest.raises(ParametersError, match="not well-formed XML"):
        parse_validate_parameters_xml(b"<Parameters><oops", default_format="application/fhir+json")


def test_operation_outcome_to_xml_shape():
    outcome = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "invalid", "diagnostics": "boom"}],
    }

    xml_bytes = operation_outcome_to_xml(outcome)

    assert xml_bytes == (
        b'<OperationOutcome xmlns="http://hl7.org/fhir">'
        b'<issue><severity value="error" /><code value="invalid" />'
        b'<diagnostics value="boom" /></issue></OperationOutcome>'
    )
