"""Preload pre-extracted FHIR IG packages into the shared package cache.

Per the FHIR Package Cache spec
(https://confluence.hl7.org/display/FHIR/FHIR+Package+Cache), the validator
(like any FHIR package client) resolves packages purely by scanning
`~/.fhir/packages` for `<packageId>#<version>/package/...` folders -- there
is no separate manifest/registration step. That means "preloading" a
package is just: make sure its already-extracted folder exists under the
cache root *before* the validator subprocess starts, so it never has to hit
the package registry over the network for it.

This module copies any such package folders found in a source directory
(default: the repo's `packages/` folder, see `Settings.packages_dir`) into
that cache root, skipping ones already present.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger("fhir_validator.package_cache")


def default_fhir_package_cache_dir() -> Path:
    """The cache root the validator itself will use (it has no override
    flag for this -- it always resolves to `$HOME/.fhir/packages`, per the
    FHIR Package Cache spec). We spawn the validator without changing its
    environment, so `Path.home()` here matches what it will see."""
    return Path.home() / ".fhir" / "packages"


def _looks_like_package_dir(path: Path) -> bool:
    return path.is_dir() and not path.name.startswith(".") and (path / "package").is_dir()


def list_cached_packages(cache_dir: Path) -> list[str]:
    """Return the `<packageId>#<version>` names of every package folder
    already present in `cache_dir` (the shared FHIR package cache), sorted
    for deterministic ordering. Empty list if `cache_dir` doesn't exist."""
    if not cache_dir.is_dir():
        return []
    return sorted(entry.name for entry in cache_dir.iterdir() if _looks_like_package_dir(entry))


def _copy_package_into_cache(entry: Path, cache_dir: Path) -> None:
    """Atomically copy a single already-extracted package folder `entry`
    into `cache_dir` under its own name. Copies to a temp path and
    atomically renames into place, so a crash/interruption mid-copy can't
    leave a half-written package folder that future runs would mistake for
    a complete one. Assumes `cache_dir / entry.name` does not already
    exist -- callers are responsible for that check."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / entry.name
    tmp_destination = cache_dir / f".preloading-{entry.name}"
    if tmp_destination.exists():
        shutil.rmtree(tmp_destination)
    shutil.copytree(entry, tmp_destination, ignore=shutil.ignore_patterns(".DS_Store"))
    tmp_destination.replace(destination)


def preload_package(name: str, source_dir: Path, cache_dir: Path) -> bool:
    """Copy a single named `<packageId>#<version>` package folder from
    `source_dir` into `cache_dir`, if it's present there and not already
    cached. Returns True if a copy actually happened as a result of this
    call, False otherwise (nothing to do: not present in `source_dir`, or
    already in `cache_dir`).

    Used both for individual `STARTUP_IGS` entries (preloaded just before
    the validator subprocess is spawned, since it resolves those itself
    with no further chance for us to intervene) and as the last-resort tier
    of `ValidatorEngine.load_configured_packages()`'s per-package fallback
    chain for `PACKAGES`/`DEFAULT_IG` (registry -> build.fhir.org ci-build
    download -> this)."""
    entry = source_dir / name
    if not _looks_like_package_dir(entry):
        return False

    destination = cache_dir / name
    if destination.exists():
        logger.debug("Package %s already in cache, skipping preload", name)
        return False

    _copy_package_into_cache(entry, cache_dir)
    logger.info("Preloaded package %s into %s", name, cache_dir)
    return True


def preload_packages(source_dir: Path, cache_dir: Path) -> list[str]:
    """Copy any package folders in `source_dir` into `cache_dir` that
    aren't already there. Returns the names actually copied. No-op
    (returns []) if `source_dir` doesn't exist."""
    if not source_dir.is_dir():
        return []

    preloaded: list[str] = []
    for entry in sorted(source_dir.iterdir()):
        if not _looks_like_package_dir(entry):
            continue

        destination = cache_dir / entry.name
        if destination.exists():
            logger.debug("Package %s already in cache, skipping preload", entry.name)
            continue

        _copy_package_into_cache(entry, cache_dir)
        logger.info("Preloaded package %s into %s", entry.name, cache_dir)
        preloaded.append(entry.name)

    return preloaded
