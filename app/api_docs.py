"""OpenAPI documentation fragments for the manually-parsed request bodies.

`$validate` and `$convert` read the raw request body themselves (see
`app/main.py`) rather than declaring a typed FastAPI `Body(...)` parameter,
because both accept *either* a JSON or an XML envelope depending on
`Content-Type`, and FastAPI's automatic request-body validation only
understands a single content type per parameter. That means Swagger/OpenAPI
docs for these two routes have to be attached by hand via each route's
`openapi_extra`/`responses` kwargs -- this module holds those fragments so
`app/main.py` stays focused on request handling.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Shared OperationOutcome schema/examples (both $validate and $convert can
# return one, either from the upstream engine or synthesized by this
# service -- see `app/main.py::_outcome_response`).
# ---------------------------------------------------------------------------

_OPERATION_OUTCOME_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "resourceType": {"type": "string", "enum": ["OperationOutcome"]},
        "issue": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {
                        "type": "string",
                        "enum": ["fatal", "error", "warning", "information"],
                    },
                    "code": {
                        "type": "string",
                        "description": (
                            "FHIR issue type code, e.g. 'invalid', 'exception', 'transient'."
                        ),
                    },
                    "diagnostics": {"type": "string"},
                },
            },
        },
    },
}


def _outcome_response(description: str, example: dict[str, Any]) -> dict[str, Any]:
    """Build an OpenAPI response object for an OperationOutcome payload.

    Representation (JSON vs XML) is chosen via `resolve_accept_format`
    (see AGENTS.md), so both content types are documented for every such
    response.
    """
    return {
        "description": description,
        "content": {
            "application/fhir+json": {"schema": _OPERATION_OUTCOME_SCHEMA, "example": example},
            "application/fhir+xml": {
                "schema": {"type": "string"},
                "description": "Same OperationOutcome, FHIR XML-encoded.",
            },
        },
    }


# ---------------------------------------------------------------------------
# POST /fhir/$validate
# ---------------------------------------------------------------------------

VALIDATE_EXAMPLE_PARAMETERS: dict[str, Any] = {
    "resourceType": "Parameters",
    "parameter": [
        {"name": "ig", "valueString": "hl7.fhir.us.core#5.0.1"},
        {
            "name": "profile",
            "valueUri": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient",
        },
        {"name": "format", "valueString": "application/fhir+json"},
        {
            "name": "resource",
            "resource": {
                "resourceType": "Patient",
                "name": [{"family": "Doe", "given": ["Jane"]}],
            },
        },
    ],
}

VALIDATE_EXAMPLE_PARAMETERS_XML = """<Parameters xmlns="http://hl7.org/fhir">
  <parameter>
    <name value="ig"/>
    <valueString value="hl7.fhir.us.core#5.0.1"/>
  </parameter>
  <parameter>
    <name value="format"/>
    <valueString value="application/fhir+xml"/>
  </parameter>
  <parameter>
    <name value="resource"/>
    <resource>
      <Patient xmlns="http://hl7.org/fhir">
        <name><family value="Doe"/></name>
      </Patient>
    </resource>
  </parameter>
</Parameters>
"""

_VALIDATE_PARAMETERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["resourceType", "parameter"],
    "description": (
        "FHIR Parameters resource. Recognized parts: 'ig' (0..*, valueString) -- IG "
        "package to load, in '<packageId>#<version>' form, e.g. "
        "'hl7.fhir.us.core#5.0.1'; 'profile' (0..*, valueUri/valueCanonical) -- profile "
        "canonical URL(s) to validate against; 'format' (0..1, valueString) -- media "
        "type of the resource being validated, e.g. 'application/fhir+json' (ignored "
        "if 'resource' is embedded inline, since an inline resource's representation "
        "always matches the request envelope); 'resource' (1..1, required) -- the "
        "resource to validate, as exactly one of an inline 'resource', a "
        "'valueString', or a 'valueBase64Binary'."
    ),
    "properties": {
        "resourceType": {"type": "string", "enum": ["Parameters"]},
        "parameter": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": ["ig", "profile", "format", "resource"],
                        "description": "Parameter name.",
                    },
                    "resource": {
                        "type": "object",
                        "description": (
                            "Inline FHIR resource. Only used for the 'resource' "
                            "parameter, when the resource being validated is embedded "
                            "directly rather than encoded as a string/base64."
                        ),
                    },
                    "valueString": {
                        "type": "string",
                        "description": (
                            "For 'ig': IG package id/canonical ('<id>#<version>'). "
                            "For 'profile': profile canonical URL. For 'format': media "
                            "type of the resource being validated (e.g. "
                            "'application/fhir+json', 'application/fhir+xml'). For "
                            "'resource': raw resource content, encoded per 'format'."
                        ),
                    },
                    "valueUri": {
                        "type": "string",
                        "description": "Alternate value encoding for 'ig'/'profile'.",
                    },
                    "valueCanonical": {
                        "type": "string",
                        "description": "Alternate value encoding for 'ig'/'profile'.",
                    },
                    "valueCode": {
                        "type": "string",
                        "description": "Alternate value encoding for 'format'.",
                    },
                    "valueUrl": {
                        "type": "string",
                        "description": "Alternate value encoding for 'ig'/'profile'.",
                    },
                    "valueBase64Binary": {
                        "type": "string",
                        "format": "byte",
                        "description": (
                            "Base64-encoded raw resource bytes, for the 'resource' "
                            "parameter, encoded per 'format'."
                        ),
                    },
                },
            },
        },
    },
}

VALIDATE_REQUEST_BODY: dict[str, Any] = {
    "required": True,
    "description": (
        "A FHIR Parameters resource, as either JSON or XML -- the envelope format is "
        "detected from this request's Content-Type header (anything containing "
        "'xml' is parsed as FHIR XML; everything else as JSON)."
    ),
    "content": {
        "application/fhir+json": {
            "schema": _VALIDATE_PARAMETERS_SCHEMA,
            "example": VALIDATE_EXAMPLE_PARAMETERS,
        },
        "application/json": {
            "schema": _VALIDATE_PARAMETERS_SCHEMA,
            "example": VALIDATE_EXAMPLE_PARAMETERS,
        },
        "application/fhir+xml": {
            "schema": {"type": "string"},
            "example": VALIDATE_EXAMPLE_PARAMETERS_XML,
        },
        "application/xml": {
            "schema": {"type": "string"},
            "example": VALIDATE_EXAMPLE_PARAMETERS_XML,
        },
    },
}

VALIDATE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: _outcome_response(
        "OperationOutcome from the validator engine describing validation results "
        "(may contain 'error'/'warning'/'information' issues even on a 200).",
        {
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "information", "code": "informational"}],
        },
    ),
    400: _outcome_response(
        "Malformed request: body is not valid JSON/XML, not a Parameters resource, "
        "missing the required 'resource' parameter, an unsupported 'format', or "
        "invalid base64 in 'valueBase64Binary'.",
        {
            "resourceType": "OperationOutcome",
            "issue": [
                {
                    "severity": "error",
                    "code": "invalid",
                    "diagnostics": "Parameters resource must include a 'resource' parameter.",
                }
            ],
        },
    ),
    502: _outcome_response(
        "The validator engine returned an error, or could not be reached.",
        {
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "error", "code": "exception", "diagnostics": "..."}],
        },
    ),
    503: _outcome_response(
        "The validator engine is not running.",
        {
            "resourceType": "OperationOutcome",
            "issue": [
                {
                    "severity": "error",
                    "code": "transient",
                    "diagnostics": "Validator engine is not running.",
                }
            ],
        },
    ),
}

# ---------------------------------------------------------------------------
# POST /fhir/$convert
# ---------------------------------------------------------------------------

CONVERT_EXAMPLE_RESOURCE_JSON: dict[str, Any] = {
    "resourceType": "Patient",
    "name": [{"family": "Doe", "given": ["Jane"]}],
}

CONVERT_EXAMPLE_RESOURCE_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Patient xmlns="http://hl7.org/fhir">'
    '<name><family value="Doe"/><given value="Jane"/></name>'
    "</Patient>"
)

CONVERT_REQUEST_BODY: dict[str, Any] = {
    "required": True,
    "description": (
        "The FHIR resource to convert, as JSON or XML -- unlike '$validate', this is "
        "the raw resource itself, not wrapped in a Parameters resource. Input format "
        "is detected from Content-Type the same way as '$validate'."
    ),
    "content": {
        "application/fhir+json": {
            "schema": {"type": "object"},
            "example": CONVERT_EXAMPLE_RESOURCE_JSON,
        },
        "application/json": {
            "schema": {"type": "object"},
            "example": CONVERT_EXAMPLE_RESOURCE_JSON,
        },
        "application/fhir+xml": {
            "schema": {"type": "string"},
            "example": CONVERT_EXAMPLE_RESOURCE_XML,
        },
        "application/xml": {
            "schema": {"type": "string"},
            "example": CONVERT_EXAMPLE_RESOURCE_XML,
        },
    },
}

CONVERT_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        # Deliberately no "content" media-type map here: Swagger UI's
        # "Execute" auto-populates an Accept header from every media type
        # listed under any *2xx* response's "content" (verified against
        # swagger-ui-dist@5's bundled request-builder), which would defeat
        # the "no Accept header -> format flip" behavior described below
        # when trying this operation out from /fhir/docs. See the example
        # payloads (CONVERT_EXAMPLE_RESOURCE_JSON/_XML above) for the
        # possible output shapes instead.
        "description": (
            "The converted resource. With no Accept header, the envelope format is "
            "flipped (JSON in -> XML out and vice versa); an explicit "
            "'Accept: application/fhir+json' or 'application/fhir+xml' overrides the "
            "flip (e.g. for a JSON->JSON pretty-print)."
        ),
    },
    400: _outcome_response(
        "Request body was empty.",
        {
            "resourceType": "OperationOutcome",
            "issue": [
                {
                    "severity": "error",
                    "code": "invalid",
                    "diagnostics": "Request body must not be empty.",
                }
            ],
        },
    ),
    500: _outcome_response(
        "The validator engine rejected the input as unconvertible. Passed through "
        "as-is from the upstream engine.",
        {
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "error", "code": "exception", "diagnostics": "..."}],
        },
    ),
    503: _outcome_response(
        "The validator engine is not running.",
        {
            "resourceType": "OperationOutcome",
            "issue": [
                {
                    "severity": "error",
                    "code": "transient",
                    "diagnostics": "Validator engine is not running.",
                }
            ],
        },
    ),
}

# ---------------------------------------------------------------------------
# GET /fhir/$packages
# ---------------------------------------------------------------------------

PACKAGES_EXAMPLE: list[dict[str, str]] = [
    {
        "name": "hl7.fhir.us.core",
        "version": "5.0.1",
        "canonicalUrl": "hl7.fhir.us.core#5.0.1",
    }
]
