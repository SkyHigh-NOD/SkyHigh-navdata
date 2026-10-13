"""Assemble the staged ``CONFIG/...`` tree into a manifest + ZIP release asset."""
from __future__ import annotations

import hashlib
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import GENERATOR_NAME, MANIFEST_NAME, REGION

SCHEMA_VERSION = 1

# Only these prefixes are ever produced or installed by SkyRadar. Keep in sync
# with the installer in skyradar/gui/dialogs/airac_update.py.
ALLOWED_PREFIXES = ("CONFIG/nav/", "CONFIG/ad/")


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
