"""XML support for the FHIR `$validate` request/response bodies.

FHIR XML encodes primitives as elements carrying a `value` attribute (e.g.
`<name value="ig"/>`) under the `http://hl7.org/fhir` namespace -- see
http://hl7.org/fhir/xml.html. This module parses a FHIR XML `Parameters`
resource into the same `ValidateRequest` shape that `app/fhir_parameters.py`
produces from JSON, so `app/main.py` can treat both envelopes identically
after parsing.

Only the specific parameters `$validate` cares about (`ig`, `profile`,
`format`, `resource`) are extracted; this is not a general-purpose FHIR
XML<->JSON converter.
"""

from __future__ import annotations

import base64
import xml.etree.ElementTree as ET

from app.fhir_parameters import ParametersError, ValidateRequest, normalize_format

FHIR_NS = "http://hl7.org/fhir"

# Re-serialize embedded resources/outcomes with the idiomatic unprefixed
# `xmlns="http://hl7.org/fhir"` rather than ElementTree's auto-generated
# `ns0:` prefix (still valid XML either way, but this matches what a FHIR
# client/validator actually emits and is easier to eyeball in logs).
ET.register_namespace("", FHIR_NS)

_VALUE_TAGS = ("valueString", "valueUri", "valueCanonical", "valueCode", "valueUrl")


def is_xml_content_type(content_type: str) -> bool:
    return "xml" in content_type.lower()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child(element: ET.Element, local_tag: str) -> ET.Element | None:
    for child in element:
        if _local_name(child.tag) == local_tag:
            return child
    return None


def _extract_value(parameter: ET.Element) -> str | None:
    for tag in _VALUE_TAGS:
        child = _child(parameter, tag)
        if child is not None:
            value = child.get("value")
            if value is not None:
                return value
    return None


def parse_validate_parameters_xml(xml_bytes: bytes, default_format: str) -> ValidateRequest:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ParametersError(f"Request body is not well-formed XML: {exc}") from exc

    if _local_name(root.tag) != "Parameters":
        raise ParametersError(
            "Request body must be a FHIR Parameters resource (root element == 'Parameters')."
        )

    igs: list[str] = []
    profiles: list[str] = []
    declared_format: str | None = None
    resource_content: bytes | None = None
    resource_is_embedded = False

    for parameter in root:
        if _local_name(parameter.tag) != "parameter":
            continue
        name_elem = _child(parameter, "name")
        name = name_elem.get("value") if name_elem is not None else None

        if name == "ig":
            value = _extract_value(parameter)
            if value:
                igs.append(value)
        elif name == "profile":
            value = _extract_value(parameter)
            if value:
                profiles.append(value)
        elif name == "format":
            value = _extract_value(parameter)
            if value:
                declared_format = value
        elif name == "resource":
            resource_elem = _child(parameter, "resource")
            if resource_elem is not None and len(resource_elem) > 0:
                # parameter.resource holds the actual resource inline, e.g.
                # <resource><Patient>...</Patient></resource>.
                resource_content = ET.tostring(resource_elem[0], encoding="utf-8")
                resource_is_embedded = True
            else:
                value_string_elem = _child(parameter, "valueString")
                base64_elem = _child(parameter, "valueBase64Binary")
                if value_string_elem is not None and value_string_elem.get("value") is not None:
                    resource_content = value_string_elem.get("value").encode("utf-8")
                elif base64_elem is not None and base64_elem.get("value") is not None:
                    try:
                        resource_content = base64.b64decode(base64_elem.get("value"))
                    except (ValueError, TypeError) as exc:
                        raise ParametersError(
                            "'resource' parameter valueBase64Binary is not valid base64."
                        ) from exc

    if resource_content is None:
        raise ParametersError(
            "Parameters resource must include a 'resource' parameter "
            "(as an inline resource, valueString, or valueBase64Binary) "
            "containing the resource to validate."
        )

    if resource_is_embedded:
        # An inline `resource` element is already structured XML, so it must
        # be forwarded as XML regardless of what "format" says; "format" in
        # that case only describes the desired response representation.
        resolved_format = "application/fhir+xml"
    else:
        resolved_format = normalize_format(declared_format) if declared_format else default_format

    return ValidateRequest(
        igs=igs,
        profiles=profiles,
        format=resolved_format,
        resource_content=resource_content,
    )


def operation_outcome_to_xml(outcome: dict) -> bytes:
    """Serialize the fixed severity/code/diagnostics shape produced by
    `app.fhir_parameters.build_operation_outcome` as FHIR XML. Not a
    general-purpose OperationOutcome serializer."""
    root = ET.Element(f"{{{FHIR_NS}}}OperationOutcome")
    for issue in outcome.get("issue", []):
        issue_elem = ET.SubElement(root, f"{{{FHIR_NS}}}issue")
        ET.SubElement(issue_elem, f"{{{FHIR_NS}}}severity", value=issue["severity"])
        ET.SubElement(issue_elem, f"{{{FHIR_NS}}}code", value=issue["code"])
        diagnostics = issue.get("diagnostics")
        if diagnostics is not None:
            ET.SubElement(issue_elem, f"{{{FHIR_NS}}}diagnostics", value=diagnostics)
    return ET.tostring(root, encoding="utf-8")
