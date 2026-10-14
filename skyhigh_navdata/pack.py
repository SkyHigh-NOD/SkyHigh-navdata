"""Assemble the staged ``CONFIG/...`` tree into a manifest + ZIP release asset."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import GENERATOR_NAME, MANIFEST_NAME, REGION

SCHEMA_VERSION = 1

# Only these prefixes are ever produced or installed by SkyRadar. Keep in sync
# with the installer in skyradar/gui/dialogs/airac_update.py.
ALLOWED_PREFIXES = ("CONFIG/nav/", "CONFIG/ad/", "CONFIG/bg/")


def allowed_relative_path(relative: str) -> bool:
    """True if a zip/manifest path is one the tool is allowed to create."""
    relative = relative.replace("\\", "/")
    if relative.startswith("/") or ".." in relative.split("/"):
        return False
    if len(relative) > 1 and relative[1] == ":":
        return False
    return relative.startswith(ALLOWED_PREFIXES)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _iter_stage_files(stage_dir: Path):
    for path in sorted(Path(stage_dir).rglob("*")):
        if path.is_file() and path.name != MANIFEST_NAME:
            yield path


# --- Local overrides --------------------------------------------------------

# Files that are never shipped even if they turn up in the overrides folder.
_OVERRIDE_SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def custom_ad_icaos(overrides_dir):
    """ICAOs with a hand-maintained ``CONFIG/ad/<ICAO>.dat`` override.

    Used to skip the X-Plane Gateway download for those aerodromes.
    """
    if not overrides_dir:
        return set()
    ad_dir = Path(overrides_dir) / "ad"
    if not ad_dir.is_dir():
        return set()
    return {path.stem.upper() for path in ad_dir.glob("*.dat") if path.is_file()}


def iter_overrides(overrides_dir):
    """Yield ``(installable relative path, source Path)`` for each override file.

    ``overrides_dir`` is a ``CONFIG`` folder; its contents map onto the ZIP's
    ``CONFIG/`` tree, e.g. ``<overrides_dir>/ad/EGSS.dat`` -> ``CONFIG/ad/EGSS.dat``.
    """
    if not overrides_dir:
        return
    overrides_dir = Path(overrides_dir)
    if not overrides_dir.is_dir():
        return
    for path in sorted(overrides_dir.rglob("*")):
        if not path.is_file():
            continue
        parts = path.relative_to(overrides_dir).parts
        if any(part.startswith(".") or part == "__pycache__" for part in parts):
            continue
        if path.name in _OVERRIDE_SKIP_NAMES:
            continue
        yield "CONFIG/" + "/".join(parts), path


def apply_overrides(stage_dir, overrides_dir, log=None) -> list:
    """Copy hand-maintained files over the generated staging tree.

    Returns the relative paths written, in the ZIP's ``CONFIG/...`` form.
    Anything outside the installable prefixes is ignored.
    """
    stage_dir = Path(stage_dir)
    applied = []
    for relative, source in iter_overrides(overrides_dir):
        if not allowed_relative_path(relative):
            if log:
                log("Ignoring override %s (not an installable path)." % relative)
            continue
        target = stage_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        applied.append(relative)
        if log:
            log("Override: %s (from %s)" % (relative, source))
    return applied


def build_manifest(stage_dir, *, region: str = REGION, airac: str = "", airac_code: str = "",
                   sources=None, counts=None, generator=GENERATOR_NAME, generator_version="") -> dict:
    stage_dir = Path(stage_dir)
    files = []
    for path in _iter_stage_files(stage_dir):
        relative = path.relative_to(stage_dir).as_posix()
        if not allowed_relative_path(relative):
            continue
        data = path.read_bytes()
        files.append({"path": relative, "size": len(data), "sha256": sha256_bytes(data)})
    manifest = {
        "schema": SCHEMA_VERSION,
        "generator": generator,
        "generator_version": generator_version,
        "region": region,
        "airac": airac,
        "airac_code": airac_code,
        "built_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": sources or {},
        "counts": counts or {},
        "files": files,
    }
    return manifest


def write_manifest(stage_dir, manifest) -> Path:
    stage_dir = Path(stage_dir)
    path = stage_dir / MANIFEST_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path


def make_zip(stage_dir, out_path: Path) -> Path:
    stage_dir = Path(stage_dir)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = out_path.with_name(out_path.name + ".tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in _iter_stage_files(stage_dir):
            relative = path.relative_to(stage_dir).as_posix()
            archive.write(path, relative)
        manifest_path = stage_dir / MANIFEST_NAME
        if manifest_path.exists():
            archive.write(manifest_path, MANIFEST_NAME)
    os.replace(temporary, out_path)
    return out_path


def asset_name(region: str, airac_code: str) -> str:
    """Release asset name, e.g. ``EG_2610.zip`` (FIR + AIRAC code).

    Every cycle gets its own asset name. SkyRadar resolves the newest asset from
    the repository's latest release rather than a fixed URL.
    """
    return "%s_%s.zip" % (region.upper(), airac_code)
