import io
import tarfile
from pathlib import Path

import httpx
import pytest

from app.ci_build import CI_BUILD_BASE_URL, download_ci_build_package, is_ci_build_version


@pytest.mark.parametrize(
    "version",
    ["0.1.1-cibuild", "3.0.0-draft", "1.0.0-ci-build", "2.0.0-screenshot", "DRAFT"],
)
def test_is_ci_build_version_true_for_draft_like_versions(version: str):
    assert is_ci_build_version(version)


@pytest.mark.parametrize("version", ["5.0.1", "2.0.0-ballot", "3.0.0"])
def test_is_ci_build_version_false_for_published_looking_versions(version: str):
    assert not is_ci_build_version(version)


def _make_package_tgz(extra_files: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        files = {"package/package.json": b'{"name": "x", "version": "1"}'}
        files.update(extra_files or {})
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_download_ci_build_package_extracts_into_cache(tmp_path: Path):
    tgz = _make_package_tgz()
    requested_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(200, content=tgz)

    cache_dir = tmp_path / "cache"
    ok = await download_ci_build_package(
        "HL7/fhir-vdor",
        "hl7.fhir.us.vdor",
        "0.1.1-cibuild",
        cache_dir,
        client=_client_for(handler),
    )

    assert ok is True
    assert requested_urls == [f"{CI_BUILD_BASE_URL}/HL7/fhir-vdor/package.tgz"]
    assert (cache_dir / "hl7.fhir.us.vdor#0.1.1-cibuild" / "package" / "package.json").exists()
    # No leftover temp dir.
    assert [p.name for p in cache_dir.iterdir()] == ["hl7.fhir.us.vdor#0.1.1-cibuild"]


async def test_download_ci_build_package_returns_false_on_404(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    cache_dir = tmp_path / "cache"
    ok = await download_ci_build_package(
        "HL7/does-not-exist",
        "hl7.fhir.us.bogus",
        "0.1.0-cibuild",
        cache_dir,
        client=_client_for(handler),
    )

    assert ok is False
    assert not cache_dir.exists() or list(cache_dir.iterdir()) == []


async def test_download_ci_build_package_returns_false_on_connection_error(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    cache_dir = tmp_path / "cache"
    ok = await download_ci_build_package(
        "HL7/fhir-vdor",
        "hl7.fhir.us.vdor",
        "0.1.1-cibuild",
        cache_dir,
        client=_client_for(handler),
    )

    assert ok is False


async def test_download_ci_build_package_returns_false_when_no_package_folder(tmp_path: Path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("readme.txt")
        data = b"not a package"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=buf.getvalue())

    cache_dir = tmp_path / "cache"
    ok = await download_ci_build_package(
        "HL7/fhir-vdor",
        "hl7.fhir.us.vdor",
        "0.1.1-cibuild",
        cache_dir,
        client=_client_for(handler),
    )

    assert ok is False
    assert not (cache_dir / "hl7.fhir.us.vdor#0.1.1-cibuild").exists()


async def test_download_ci_build_package_returns_false_on_malformed_archive(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not a valid tarball")

    cache_dir = tmp_path / "cache"
    ok = await download_ci_build_package(
        "HL7/fhir-vdor",
        "hl7.fhir.us.vdor",
        "0.1.1-cibuild",
        cache_dir,
        client=_client_for(handler),
    )

    assert ok is False


async def test_download_ci_build_package_overwrites_stale_cache_entry(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    stale = cache_dir / "hl7.fhir.us.vdor#0.1.1-cibuild" / "package"
    stale.mkdir(parents=True)
    (stale / "package.json").write_text("stale")

    tgz = _make_package_tgz()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=tgz)

    ok = await download_ci_build_package(
        "HL7/fhir-vdor",
        "hl7.fhir.us.vdor",
        "0.1.1-cibuild",
        cache_dir,
        client=_client_for(handler),
    )

    assert ok is True
    package_json = cache_dir / "hl7.fhir.us.vdor#0.1.1-cibuild" / "package" / "package.json"
    assert package_json.read_text() != "stale"
