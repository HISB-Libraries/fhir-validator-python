from pathlib import Path

from app.package_cache import preload_packages


def _make_package_dir(root: Path, name: str, with_stray_file: bool = False) -> Path:
    pkg_dir = root / name
    (pkg_dir / "package").mkdir(parents=True)
    (pkg_dir / "package" / "package.json").write_text('{"name": "x", "version": "1"}')
    (pkg_dir / "package" / ".index.json").write_text("{}")
    if with_stray_file:
        (pkg_dir / ".DS_Store").write_bytes(b"\x00\x01")
    return pkg_dir


def test_preload_copies_packages_not_already_cached(tmp_path: Path):
    source = tmp_path / "packages"
    cache = tmp_path / "cache"
    _make_package_dir(source, "hl7.fhir.us.core#5.0.1")

    preloaded = preload_packages(source, cache)

    assert preloaded == ["hl7.fhir.us.core#5.0.1"]
    assert (cache / "hl7.fhir.us.core#5.0.1" / "package" / "package.json").exists()


def test_preload_skips_packages_already_in_cache(tmp_path: Path):
    source = tmp_path / "packages"
    cache = tmp_path / "cache"
    _make_package_dir(source, "hl7.fhir.us.core#5.0.1")
    # Simulate it already being cached, with different content than the source
    # copy, to prove preload doesn't clobber an existing cache entry.
    existing = cache / "hl7.fhir.us.core#5.0.1" / "package"
    existing.mkdir(parents=True)
    (existing / "package.json").write_text("already here")

    preloaded = preload_packages(source, cache)

    assert preloaded == []
    assert (cache / "hl7.fhir.us.core#5.0.1" / "package" / "package.json").read_text() == (
        "already here"
    )


def test_preload_copies_multiple_packages_and_ignores_non_package_entries(tmp_path: Path):
    source = tmp_path / "packages"
    cache = tmp_path / "cache"
    _make_package_dir(source, "hl7.fhir.us.core#5.0.1")
    _make_package_dir(source, "hl7.fhir.uv.ips#2.0.0")
    (source / ".DS_Store").write_bytes(b"\x00\x01")  # stray file, not a package dir
    (source / "not-a-package").mkdir()  # dir without a package/ subfolder

    preloaded = preload_packages(source, cache)

    assert sorted(preloaded) == ["hl7.fhir.us.core#5.0.1", "hl7.fhir.uv.ips#2.0.0"]
    assert not (cache / ".DS_Store").exists()
    assert not (cache / "not-a-package").exists()


def test_preload_strips_ds_store_from_copied_packages(tmp_path: Path):
    source = tmp_path / "packages"
    cache = tmp_path / "cache"
    _make_package_dir(source, "hl7.fhir.us.core#5.0.1", with_stray_file=True)

    preload_packages(source, cache)

    assert not (cache / "hl7.fhir.us.core#5.0.1" / ".DS_Store").exists()
    assert (cache / "hl7.fhir.us.core#5.0.1" / "package" / "package.json").exists()


def test_preload_is_noop_when_source_dir_missing(tmp_path: Path):
    preloaded = preload_packages(tmp_path / "does-not-exist", tmp_path / "cache")

    assert preloaded == []
    assert not (tmp_path / "cache").exists()


def test_preload_does_not_leave_tmp_dir_behind(tmp_path: Path):
    source = tmp_path / "packages"
    cache = tmp_path / "cache"
    _make_package_dir(source, "hl7.fhir.us.core#5.0.1")

    preload_packages(source, cache)

    leftovers = [p.name for p in cache.iterdir()]
    assert leftovers == ["hl7.fhir.us.core#5.0.1"]
