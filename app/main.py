"""FastAPI application exposing the FHIR `$validate` operation.

Endpoints:
  - POST /fhir/$validate  (base = hostname/fhir, per AGENTS.md)
  - POST /fhir/$convert   (JSON<->XML resource format conversion)
  - GET  /fhir/$packages  (advertises the `PACKAGES` env var, see app/config.py)
  - GET  /healthz

Startup pattern
----------------
On app startup, a single persistent `ValidatorEngine` (see
app/validator_engine.py) is created and, unless `AUTO_START_VALIDATOR=false`,
launched. It is stored on `app.state.validator_engine` so request handlers
and tests can reach it without a global singleton.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.api_docs import (
    CONVERT_REQUEST_BODY,
    CONVERT_RESPONSES,
    PACKAGES_EXAMPLE,
    VALIDATE_REQUEST_BODY,
    VALIDATE_RESPONSES,
)
from app.config import Settings
from app.fhir_parameters import (
    ParametersError,
    build_operation_outcome,
    parse_validate_parameters,
    resolve_accept_format,
)
from app.fhir_xml import (
    is_xml_content_type,
    operation_outcome_to_xml,
    parse_validate_parameters_xml,
)
from app.validator_engine import ValidatorEngine, ValidatorEngineError

logger = logging.getLogger("fhir_validator")


def _outcome_response(
    status_code: int, severity: str, code: str, diagnostics: str, response_format: str
) -> Response:
    outcome = build_operation_outcome(severity, code, diagnostics)
    if is_xml_content_type(response_format):
        content = operation_outcome_to_xml(outcome)
    else:
        content = json.dumps(outcome).encode("utf-8")
    return Response(content=content, status_code=status_code, media_type=response_format)


def _package_summary(canonical: str) -> dict:
    """Split a `<packageId>#<version>` string (package ids can't contain
    "#", per the FHIR Package Cache spec) into the shape `GET /fhir/$packages`
    returns."""
    name, _, version = canonical.partition("#")
    return {"name": name, "version": version, "canonicalUrl": canonical}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    engine = ValidatorEngine(settings)
    app.state.validator_engine = engine
    if settings.auto_start_validator:
        await engine.start()
    try:
        yield
    finally:
        await engine.stop()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    logging.basicConfig(level=settings.log_level)

    app = FastAPI(
        title="FHIR Validation Service",
        description=(
            "FHIR Validation Service API fronting the HL7 FHIR Validator CLI "
            "(`validator_cli.jar`), exposing `$validate`, `$convert`, and "
            "`$packages` operations. See the project's AGENTS.md for the full "
            "architecture and request contract writeup."
        ),
        lifespan=lifespan,
        docs_url="/fhir/docs",
        redoc_url="/fhir/redoc",
        openapi_url="/fhir/openapi.json",
        openapi_tags=[
            {
                "name": "Validation",
                "description": "Validate a FHIR resource against loaded IGs/profiles.",
            },
            {
                "name": "Conversion",
                "description": "Convert a FHIR resource between its JSON and XML representations.",
            },
            {
                "name": "Packages",
                "description": "Inspect the FHIR IG packages configured for this deployment.",
            },
            {"name": "Health", "description": "Service/engine liveness."},
        ],
    )
    app.state.settings = settings

    # Enables CORS preflight `OPTIONS` handling on every route (Starlette's
    # CORSMiddleware intercepts `OPTIONS` requests carrying an
    # `Access-Control-Request-Method` header before they reach any endpoint,
    # so routes above don't need their own `OPTIONS` handlers).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get(
        "/healthz",
        tags=["Health"],
        summary="Liveness/readiness check",
        description="Reports whether the persistent validator engine subprocess is running.",
    )
    async def healthz(request: Request) -> dict:
        engine: ValidatorEngine = request.app.state.validator_engine
        return await engine.health()

    @app.get(
        "/fhir/$packages",
        tags=["Packages"],
        summary="List configured FHIR IG packages",
        description=(
            "Returns the packages listed in the `PACKAGES` env var (see AGENTS.md), "
            "in the order given. Pure config reflection -- it doesn't touch the "
            "validator engine, so it responds even if the engine isn't running."
        ),
        responses={
            200: {
                "description": "The configured packages.",
                "content": {"application/json": {"example": PACKAGES_EXAMPLE}},
            }
        },
    )
    async def packages(request: Request) -> list[dict]:
        settings: Settings = request.app.state.settings
        return [_package_summary(pkg) for pkg in settings.packages_list]

    @app.post(
        "/fhir/$validate",
        tags=["Validation"],
        summary="Validate a FHIR resource",
        description=(
            "Validates a FHIR resource against the FHIR specification and, "
            "optionally, one or more IG profiles. The request body is a FHIR "
            "`Parameters` resource (see requestBody below) specifying the IG(s) "
            "to load, the profile(s) to validate against, and the resource itself. "
            "Response representation (JSON/XML) is chosen from the `Accept` header, "
            "falling back to the request envelope's own format."
        ),
        openapi_extra={"requestBody": VALIDATE_REQUEST_BODY},
        responses=VALIDATE_RESPONSES,
    )
    async def validate(request: Request) -> Response:
        engine: ValidatorEngine = request.app.state.validator_engine
        settings: Settings = request.app.state.settings

        request_is_xml = is_xml_content_type(request.headers.get("content-type", ""))
        # Default resource-content-type fallback matches the envelope's own
        # representation; an explicit "format" parameter or Accept header
        # always takes precedence over this.
        default_resource_format = (
            "application/fhir+xml" if request_is_xml else settings.default_response_format
        )
        response_format = resolve_accept_format(
            request.headers.get("accept", ""), default_resource_format
        )

        body = await request.body()

        try:
            if request_is_xml:
                validate_request = parse_validate_parameters_xml(body, default_resource_format)
            else:
                try:
                    payload = json.loads(body)
                except ValueError:
                    return _outcome_response(
                        400, "error", "invalid", "Request body is not valid JSON.", response_format
                    )
                validate_request = parse_validate_parameters(payload, default_resource_format)
        except ParametersError as exc:
            return _outcome_response(400, "error", "invalid", str(exc), response_format)

        if not engine.is_running:
            return _outcome_response(
                503, "error", "transient", "Validator engine is not running.", response_format
            )

        try:
            await engine.ensure_igs_loaded(validate_request.igs)
            upstream = await engine.validate_resource(
                content=validate_request.resource_content,
                content_type=validate_request.format,
                profiles=validate_request.profiles,
                accept=response_format,
            )
        except ValidatorEngineError as exc:
            return _outcome_response(502, "error", "exception", str(exc), response_format)
        except httpx.HTTPError as exc:
            return _outcome_response(
                502,
                "error",
                "exception",
                f"Could not reach validator engine: {exc}",
                response_format,
            )

        content_type = upstream.headers.get("content-type", response_format)
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=content_type,
        )

    @app.post(
        "/fhir/$convert",
        tags=["Conversion"],
        summary="Convert a FHIR resource between JSON and XML",
        description=(
            "Converts a FHIR resource between its JSON and XML representations. "
            "Unlike `$validate`, the body is the raw resource itself (no `Parameters` "
            "wrapper). With no `Accept` header, the envelope format is flipped "
            "(JSON in gives XML out and vice versa); an explicit `Accept` header "
            "overrides the flip."
        ),
        openapi_extra={"requestBody": CONVERT_REQUEST_BODY},
        responses=CONVERT_RESPONSES,
    )
    async def convert(request: Request) -> Response:
        engine: ValidatorEngine = request.app.state.validator_engine

        request_is_xml = is_xml_content_type(request.headers.get("content-type", ""))
        input_format = "application/fhir+xml" if request_is_xml else "application/fhir+json"
        opposite_format = "application/fhir+json" if request_is_xml else "application/fhir+xml"
        # Unlike $validate, the underlying engine's own /convert defaults to
        # JSON when Accept is omitted (it does not infer "the other format"
        # from Content-Type) -- so we always resolve an explicit target here,
        # falling back to the JSON<->XML flip rather than the engine's own
        # default.
        target_format = resolve_accept_format(request.headers.get("accept", ""), opposite_format)

        body = await request.body()
        if not body:
            return _outcome_response(
                400, "error", "invalid", "Request body must not be empty.", target_format
            )

        if not engine.is_running:
            return _outcome_response(
                503, "error", "transient", "Validator engine is not running.", target_format
            )

        try:
            upstream = await engine.convert_resource(
                content=body, content_type=input_format, accept=target_format
            )
        except httpx.HTTPError as exc:
            return _outcome_response(
                502,
                "error",
                "exception",
                f"Could not reach validator engine: {exc}",
                target_format,
            )

        content_type = upstream.headers.get("content-type", target_format)
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=content_type,
        )

    return app


app = create_app()
