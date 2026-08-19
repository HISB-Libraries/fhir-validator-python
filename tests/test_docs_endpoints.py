import re

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_docs_urls_default_to_fhir_prefix_only():
    app = create_app(Settings(auto_start_validator=False))

    with TestClient(app) as test_client:
        docs_response = test_client.get("/fhir/docs")
        openapi_response = test_client.get("/fhir/openapi.json")

    assert docs_response.status_code == 200
    assert openapi_response.status_code == 200


def test_docs_urls_reachable_unprefixed_when_custom_path_set():
    # `custom_path` is applied via `root_path` (informational only, see
    # app/main.py::create_app), not baked into route registration -- so the
    # routes stay reachable at their bare path. This is what makes them work
    # behind a reverse proxy that *strips* its mount prefix before
    # forwarding (the container only ever sees `/fhir/docs`, never
    # `/my-service/fhir/docs`) -- the same way `/fhir/$validate` etc already
    # do, prefixed or not.
    app = create_app(Settings(auto_start_validator=False, custom_path="my-service"))

    with TestClient(app) as test_client:
        docs_response = test_client.get("/fhir/docs")
        redoc_response = test_client.get("/fhir/redoc")
        openapi_response = test_client.get("/fhir/openapi.json")

    assert docs_response.status_code == 200
    assert redoc_response.status_code == 200
    assert openapi_response.status_code == 200


def test_docs_urls_also_reachable_with_custom_path_prefix_in_the_request():
    # Starlette's routing strips a leading `root_path` from the incoming
    # request path if present (`get_route_path`) -- so a reverse proxy that
    # forwards the full path *unstripped* (container itself sees
    # `/my-service/fhir/docs`) is transparently supported too, without any
    # extra code.
    app = create_app(Settings(auto_start_validator=False, custom_path="my-service"))

    with TestClient(app) as test_client:
        docs_response = test_client.get("/my-service/fhir/docs")
        openapi_response = test_client.get("/my-service/fhir/openapi.json")

    assert docs_response.status_code == 200
    assert openapi_response.status_code == 200


def test_docs_html_embeds_custom_path_prefixed_openapi_url():
    # Regression test for the actual reported bug: Swagger UI's "Failed to
    # load API definition" / "Fetch error Not Found /fhir/openapi.json" --
    # caused by the docs page's self-referencing `openapi_url` missing the
    # reverse proxy's mount prefix. Requesting `/fhir/docs` unprefixed here
    # simulates a proxy that strips `/my-service` before forwarding (see
    # test_docs_urls_reachable_unprefixed_when_custom_path_set above) --
    # exactly the deployment this bug was reported from.
    app = create_app(Settings(auto_start_validator=False, custom_path="my-service"))

    with TestClient(app) as test_client:
        docs_html = test_client.get("/fhir/docs").text
        redoc_html = test_client.get("/fhir/redoc").text

    docs_match = re.search(r"url: *'([^']+)'", docs_html)
    assert docs_match is not None
    assert docs_match.group(1) == "/my-service/fhir/openapi.json"

    assert 'spec-url="/my-service/fhir/openapi.json"' in redoc_html


def test_docs_urls_normalize_leading_and_trailing_slashes_in_custom_path():
    app = create_app(Settings(auto_start_validator=False, custom_path="/my-service/"))

    with TestClient(app) as test_client:
        docs_response = test_client.get("/fhir/docs")

    assert docs_response.status_code == 200


def test_openapi_schema_has_no_servers_entry_without_custom_path():
    app = create_app(Settings(auto_start_validator=False))

    with TestClient(app) as test_client:
        schema = test_client.get("/fhir/openapi.json").json()

    assert "servers" not in schema


def test_openapi_schema_declares_custom_path_as_server():
    # So Swagger UI's "Try it out"/"Execute" (and any client generated from
    # the spec) call the actual operations (`/fhir/$validate` etc, which are
    # -- unlike the docs endpoints -- never prefixed at the route level
    # regardless of `custom_path`) under the reverse proxy's mount prefix
    # too, instead of resolving them relative to the origin root.
    app = create_app(Settings(auto_start_validator=False, custom_path="my-service"))

    with TestClient(app) as test_client:
        schema = test_client.get("/fhir/openapi.json").json()

    assert schema["servers"] == [{"url": "/my-service"}]
