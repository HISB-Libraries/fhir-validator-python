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


def test_docs_urls_prepend_custom_path_when_set():
    app = create_app(Settings(auto_start_validator=False, custom_path="my-service"))

    with TestClient(app) as test_client:
        docs_response = test_client.get("/my-service/fhir/docs")
        redoc_response = test_client.get("/my-service/fhir/redoc")
        openapi_response = test_client.get("/my-service/fhir/openapi.json")
        unprefixed_response = test_client.get("/fhir/docs")

    assert docs_response.status_code == 200
    assert redoc_response.status_code == 200
    assert openapi_response.status_code == 200
    assert unprefixed_response.status_code == 404


def test_docs_urls_normalize_leading_and_trailing_slashes_in_custom_path():
    app = create_app(Settings(auto_start_validator=False, custom_path="/my-service/"))

    with TestClient(app) as test_client:
        docs_response = test_client.get("/my-service/fhir/docs")

    assert docs_response.status_code == 200
