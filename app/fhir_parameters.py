"""Parsing/serialization helpers for the FHIR `$validate` operation payload.

Per AGENTS.md, the request body is a FHIR `Parameters` resource with:
  - "ig"       (0..*) IG package id/canonical to load, e.g. "hl7.fhir.us.core#5.0.1"
  - "profile"  (0..*) profile canonical URL(s) to validate against
  - "format"   (0..1) media type of the resource being validated
  - "resource" (1..1) the resource to validate

The "resource" parameter may carry the resource in one of three ways:
  - `parameter.resource`        -- an inline FHIR resource (always effectively
                                    JSON, since it's parsed as part of the
                                    JSON request body)
  - `parameter.valueString`     -- raw resource content as a string, encoded
                                    per "format" (e.g. raw XML)
  - `parameter.valueBase64Binary` -- raw resource bytes, encoded per "format"

Both JSON and XML outer `Parameters` envelopes are supported; this module
handles JSON (see `app/fhir_xml.py` for the equivalent XML parsing, which
produces the same `ValidateRequest` shape).
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field

# Media types accepted for the *inner* resource being validated, normalized
# to the `application/fhir+*` form the validator's HTTP service expects.
_FORMAT_ALIASES = {
    "json": "application/fhir+json",
    "application/json": "application/fhir+json",
    "application/fhir+json": "application/fhir+json",
    "xml": "application/fhir+xml",
    "application/xml": "application/fhir+xml",
    "application/fhir+xml": "application/fhir+xml",
}

_VALUE_KEYS = ("valueString", "valueUri", "valueCanonical", "valueCode", "valueUrl")


class ParametersError(ValueError):
    """Raised when the request body is not a well-formed `$validate` Parameters resource."""


@dataclass
class ValidateRequest:
    igs: list[str] = field(default_factory=list)
    profiles: list[str] = field(default_factory=list)
    format: str = "application/fhir+json"
    resource_content: bytes = b""


def normalize_format(value: str) -> str:
    normalized = _FORMAT_ALIASES.get(value.strip().lower())
    if normalized is None:
        raise ParametersError(
            f"Unsupported format '{value}'. Expected one of: "
            "application/json, application/fhir+json, application/xml, application/fhir+xml"
        )
    return normalized


def resolve_accept_format(accept_header: str, default_format: str) -> str:
    """Pick a response representation from an `Accept` header.

    Only exact (post-`;q=...` stripping) matches against the known format
    aliases are honored; wildcards (`*/*`) and unrecognized types fall back
    to `default_format` rather than attempting full HTTP content
    negotiation.
    """
    for candidate in accept_header.split(","):
        candidate = candidate.split(";", 1)[0].strip()
        if not candidate or candidate == "*/*":
            continue
        try:
            return normalize_format(candidate)
        except ParametersError:
            continue
    return default_format


def _extract_value(entry: dict) -> str | None:
    for key in _VALUE_KEYS:
        if key in entry and entry[key] is not None:
            return str(entry[key])
    return None


def parse_validate_parameters(payload: dict, default_format: str) -> ValidateRequest:
    if not isinstance(payload, dict):
        raise ParametersError("Request body must be a JSON object.")
    if payload.get("resourceType") != "Parameters":
        raise ParametersError(
            "Request body must be a FHIR Parameters resource (resourceType == 'Parameters')."
        )

    igs: list[str] = []
    profiles: list[str] = []
    declared_format: str | None = None
    resource_content: bytes | None = None
    resource_is_embedded = False

    for entry in payload.get("parameter") or []:
        name = entry.get("name")
        if name == "ig":
            value = _extract_value(entry)
            if value:
                igs.append(value)
        elif name == "profile":
            value = _extract_value(entry)
            if value:
                profiles.append(value)
        elif name == "format":
            value = _extract_value(entry)
            if value:
                declared_format = value
        elif name == "resource":
            if "resource" in entry and entry["resource"] is not None:
                # Pretty-print (rather than json.dumps' default single-line
                # minified output) so the validator engine's OperationOutcome
                # issues carry meaningful line numbers -- a minified resource
                # puts every issue on "line 1", which is useless for locating
                # the actual problem in the original resource.
                resource_content = json.dumps(entry["resource"], indent=2).encode("utf-8")
                resource_is_embedded = True
            elif "valueString" in entry and entry["valueString"] is not None:
                resource_content = entry["valueString"].encode("utf-8")
            elif "valueBase64Binary" in entry and entry["valueBase64Binary"] is not None:
                try:
                    resource_content = base64.b64decode(entry["valueBase64Binary"])
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
        # An inline `resource` part was already parsed as JSON along with the
        # rest of the request body, so it must be forwarded as JSON
        # regardless of what "format" says; "format" in that case only
        # describes the desired response representation.
        resolved_format = "application/fhir+json"
    else:
        resolved_format = normalize_format(declared_format) if declared_format else default_format

    return ValidateRequest(
        igs=igs,
        profiles=profiles,
        format=resolved_format,
        resource_content=resource_content,
    )


def build_operation_outcome(severity: str, code: str, diagnostics: str) -> dict:
    return {
        "resourceType": "OperationOutcome",
        "issue": [
            {
                "severity": severity,
                "code": code,
                "diagnostics": diagnostics,
            }
        ],
    }
