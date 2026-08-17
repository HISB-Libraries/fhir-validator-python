import base64

import pytest

from app.fhir_parameters import ParametersError, build_operation_outcome, parse_validate_parameters


def test_parses_inline_resource_and_repeating_ig_and_profile():
    payload = {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "ig", "valueString": "hl7.fhir.us.core#5.0.1"},
            {"name": "ig", "valueString": "hl7.fhir.uv.ips"},
            {
                "name": "profile",
                "valueUri": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient",
            },
            {"name": "format", "valueCode": "application/json"},
            {
                "name": "resource",
                "resource": {"resourceType": "Patient", "name": [{"family": "Doe"}]},
            },
        ],
    }

    result = parse_validate_parameters(payload, default_format="application/fhir+json")

    assert result.igs == ["hl7.fhir.us.core#5.0.1", "hl7.fhir.uv.ips"]
    assert result.profiles == ["http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient"]
    # Inline resources are always forwarded as JSON regardless of declared format.
    assert result.format == "application/fhir+json"
    assert b'"resourceType": "Patient"' in result.resource_content


def test_parses_raw_string_resource_with_declared_xml_format():
    payload = {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "format", "valueCode": "application/xml"},
            {
                "name": "resource",
                "valueString": "<Patient xmlns='http://hl7.org/fhir'></Patient>",
            },
        ],
    }

    result = parse_validate_parameters(payload, default_format="application/fhir+json")

    assert result.format == "application/fhir+xml"
    assert result.resource_content == b"<Patient xmlns='http://hl7.org/fhir'></Patient>"


def test_parses_base64_binary_resource():
    raw = b'{"resourceType":"Patient"}'
    payload = {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "resource", "valueBase64Binary": base64.b64encode(raw).decode()},
        ],
    }

    result = parse_validate_parameters(payload, default_format="application/fhir+json")

    assert result.resource_content == raw
    assert result.format == "application/fhir+json"


def test_defaults_format_when_not_declared():
    payload = {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "resource", "valueString": "not-actually-checked"},
        ],
    }

    result = parse_validate_parameters(payload, default_format="application/fhir+xml")

    assert result.format == "application/fhir+xml"


def test_rejects_non_parameters_resource():
    with pytest.raises(ParametersError, match="Parameters"):
        parse_validate_parameters(
            {"resourceType": "Patient"}, default_format="application/fhir+json"
        )


def test_rejects_missing_resource_parameter():
    payload = {
        "resourceType": "Parameters",
        "parameter": [{"name": "ig", "valueString": "hl7.fhir.uv.ips"}],
    }
    with pytest.raises(ParametersError, match="resource"):
        parse_validate_parameters(payload, default_format="application/fhir+json")


def test_rejects_unsupported_format():
    payload = {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "format", "valueCode": "text/plain"},
            {"name": "resource", "valueString": "x"},
        ],
    }
    with pytest.raises(ParametersError, match="Unsupported format"):
        parse_validate_parameters(payload, default_format="application/fhir+json")


def test_build_operation_outcome_shape():
    outcome = build_operation_outcome("error", "invalid", "boom")
    assert outcome == {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "invalid", "diagnostics": "boom"}],
    }
