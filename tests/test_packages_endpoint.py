from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

PACKAGES_URL = "/fhir/$packages"


def test_packages_returns_default_list_in_order(client):
    response = client.get(PACKAGES_URL)

    assert response.status_code == 200
    assert response.json() == [
        {
            "name": "hl7.fhir.us.vr-common-library",
            "version": "2.0.0",
            "canonicalUrl": "hl7.fhir.us.vr-common-library#2.0.0",
        },
        {
            "name": "hl7.fhir.us.vdor",
            "version": "0.1.1-cibuild",
            "canonicalUrl": "hl7.fhir.us.vdor#0.1.1-cibuild",
        },
        {
            "name": "hl7.fhir.us.mdi",
            "version": "3.0.0-draft",
            "canonicalUrl": "hl7.fhir.us.mdi#3.0.0-draft",
        },
        {
            "name": "hl7.fhir.us.mdi",
            "version": "2.0.0",
            "canonicalUrl": "hl7.fhir.us.mdi#2.0.0",
        },
        {
            "name": "hl7.fhir.us.bser",
            "version": "2.0.0-ballot",
            "canonicalUrl": "hl7.fhir.us.bser#2.0.0-ballot",
        },
        {
            "name": "hl7.fhir.us.vrdr",
            "version": "3.0.0",
            "canonicalUrl": "hl7.fhir.us.vrdr#3.0.0",
        },
        {
            "name": "hl7.fhir.us.core",
            "version": "5.0.1",
            "canonicalUrl": "hl7.fhir.us.core#5.0.1",
        },
    ]


def test_packages_respects_custom_env_var():
    settings = Settings(auto_start_validator=False, packages="foo.bar#1.0.0,baz.qux#2.3.4")
    app = create_app(settings)

    with TestClient(app) as test_client:
        response = test_client.get(PACKAGES_URL)

    assert response.status_code == 200
    assert response.json() == [
        {"name": "foo.bar", "version": "1.0.0", "canonicalUrl": "foo.bar#1.0.0"},
        {"name": "baz.qux", "version": "2.3.4", "canonicalUrl": "baz.qux#2.3.4"},
    ]


def test_packages_handles_missing_hash_gracefully():
    settings = Settings(auto_start_validator=False, packages="no-version-here")
    app = create_app(settings)

    with TestClient(app) as test_client:
        response = test_client.get(PACKAGES_URL)

    assert response.json() == [
        {"name": "no-version-here", "version": "", "canonicalUrl": "no-version-here"}
    ]


def test_packages_empty_env_var_returns_empty_list():
    settings = Settings(auto_start_validator=False, packages="")
    app = create_app(settings)

    with TestClient(app) as test_client:
        response = test_client.get(PACKAGES_URL)

    assert response.status_code == 200
    assert response.json() == []


def test_packages_does_not_require_validator_engine_running(client, fake_engine):
    fake_engine.is_running = False

    response = client.get(PACKAGES_URL)

    assert response.status_code == 200
    assert len(response.json()) == 7
