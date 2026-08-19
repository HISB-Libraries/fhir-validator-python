from app.config import Settings


def test_ci_build_repos_map_parses_entries():
    settings = Settings(
        ci_build_repos=(
            "hl7.fhir.us.vdor#0.1.1-cibuild=HL7/fhir-vdor,"
            "hl7.fhir.us.mdi#3.0.0-draft=HL7/fhir-mdi-ig"
        )
    )

    assert settings.ci_build_repos_map == {
        "hl7.fhir.us.vdor#0.1.1-cibuild": "HL7/fhir-vdor",
        "hl7.fhir.us.mdi#3.0.0-draft": "HL7/fhir-mdi-ig",
    }


def test_ci_build_repos_map_empty_when_unset():
    assert Settings(ci_build_repos="").ci_build_repos_map == {}


def test_ci_build_repos_map_ignores_malformed_entries():
    settings = Settings(ci_build_repos="no-equals-sign, =missing-package, pkg#1.0=")

    assert settings.ci_build_repos_map == {}


def test_ci_build_repos_map_strips_whitespace():
    settings = Settings(ci_build_repos=" pkg#1.0-draft = HL7/some-repo ")

    assert settings.ci_build_repos_map == {"pkg#1.0-draft": "HL7/some-repo"}


def test_custom_path_normalized_empty_by_default():
    assert Settings().custom_path_normalized == ""


def test_custom_path_normalized_adds_leading_slash():
    assert Settings(custom_path="fhir-validator").custom_path_normalized == "/fhir-validator"


def test_custom_path_normalized_strips_leading_and_trailing_slashes():
    assert Settings(custom_path="/fhir-validator/").custom_path_normalized == "/fhir-validator"


def test_custom_path_normalized_blank_string_is_empty():
    assert Settings(custom_path="   ").custom_path_normalized == ""
