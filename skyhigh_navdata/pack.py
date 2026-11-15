"""Assemble the staged ``CONFIG/...`` tree into a manifest + ZIP release asset."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import GENERATOR_NAME, MANIFEST_NAME, REGION

SCHEMA_VERSION = 1

# Only these prefixes are ever produced or installed by SkyRadar. Keep in sync
# with the installer in skyradar/gui/dialogs/airac_update.py.
ALLOWED_PREFIXES = ("CONFIG/nav/", "CONFIG/ad/", "CONFIG/bg/")

REPO_ROOT = Path(__file__).resolve().parent.parent

# An absolute path inside a message: a drive or UNC path, or one from the
# root. A URL and a relative path (dist/EG_2611.zip) do not match.
_ABSOLUTE_PATH_RE = re.compile(
    r"""(?<![\w:/.\\-])(?:[A-Za-z]:[\\/]|\\\\|/)(?:[^\s'"<>|*?\\/]+[\\/]+)+([^\s'"<>|*?\\/]*)""")


def display_path(path) -> str:
    """A path as a message may show it: never absolute.

    Relative to the working directory or the repository when it is inside
    one of them, and the base name alone otherwise. Messages end up in
    terminals, logs and pasted bug reports; where the builder keeps its files
    is nobody else's business.
    """
    path = Path(path)
    if not path.is_absolute():
        return path.as_posix()
    for root in (Path.cwd(), REPO_ROOT):
        try:
            return path.resolve().relative_to(root.resolve()).as_posix() or "."
        except (OSError, ValueError):
            continue
    return path.name or "."


# A quoted absolute path, which may hold spaces: the quotes say where it ends.
_QUOTED_PATH_RE = re.compile(r"""(['"])((?:[A-Za-z]:[\\/]|\\\\|/)[^'"\n]*)\1""")


def _private_roots() -> list:
    """Folders whose location must not show: longest first, in both slash forms."""
    found = set()
    for get in (Path.home, Path.cwd, lambda: REPO_ROOT, lambda: Path(tempfile.gettempdir())):
        try:
            root = get()
            for form in (str(root), root.as_posix(), str(root.resolve()), root.resolve().as_posix()):
                if len(form) > 3:
                    found.add(form)
        except (OSError, RuntimeError):
            continue
    return sorted(found, key=len, reverse=True)


def scrub_paths(text) -> str:
    """``text`` with every absolute path in it cut down to its base name.

    Three passes, because a path with a space in it has no end a pattern can
    find: quoted paths first, then anything under a folder this tool knows
    (home, working directory, repository, temp), then whatever still looks
    like an absolute path.
    """
    text = str(text)

    def base_name(match):
        name = re.split(r"[\\/]+", match.group(2).rstrip("\\/"))[-1]
        return "%s%s%s" % (match.group(1), name or "(folder)", match.group(1))

    text = _QUOTED_PATH_RE.sub(base_name, text)
    for root in _private_roots():
        text = re.sub(re.escape(root) + r"[\\/]+", "", text, flags=re.IGNORECASE)
        text = re.sub(re.escape(root), "(folder)", text, flags=re.IGNORECASE)
    return _ABSOLUTE_PATH_RE.sub(lambda match: match.group(1) or "(folder)", text)


def error_text(error) -> str:
    """What went wrong, in words fit for a message: no absolute path.

    An OSError names its file by the full path. Here it is the reason and the
    file's base name; any other error has the paths in its text scrubbed.
    """
    reason = getattr(error, "strerror", None)
    filename = getattr(error, "filename", None)
    if isinstance(error, OSError) and isinstance(reason, str) and reason and filename:
        names = [Path(str(name)).name for name in (filename, getattr(error, "filename2", None)) if name]
        return scrub_paths("%s: %s" % (reason, " -> ".join(names)))
    return scrub_paths(error)


def allowed_relative_path(relative: str) -> bool:
    """True if a zip/manifest path is one the tool is allowed to create."""
    relative = relative.replace("\\", "/")
    if relative.startswith("/") or ".." in relative.split("/"):
        return False
    if len(relative) > 1 and relative[1] == ":":
        return False
    return relative.startswith(ALLOWED_PREFIXES)


def safe_extract(archive: zipfile.ZipFile, dest_dir) -> None:
    """Extract an archive, refusing any member that would land outside ``dest_dir``."""
    dest_dir = Path(dest_dir)
    root = dest_dir.resolve()
    for member in archive.infolist():
        name = member.filename.replace("\\", "/")
        target = (root / name).resolve()
        if name.startswith("/") or (len(name) > 1 and name[1] == ":") \
                or ".." in name.split("/") or (target != root and root not in target.parents):
            raise ValueError("archive member escapes the extraction folder: %r" % member.filename)
    archive.extractall(dest_dir)


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
            log("Override: %s" % relative)
    return applied


def build_manifest(stage_dir, *, region: str = REGION, airac: str = "", airac_code: str = "",
                   sources=None, counts=None, generator=GENERATOR_NAME, generator_version="",
                   overrides=None) -> dict:
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
        "overrides": list(overrides or []),
        "files": files,
    }
    return manifest


# A drive path (C:\ or C:/) or a Unix home folder anywhere in a string. The
# drive letter stands alone, so the "s:/" inside "https://" is not one.
_LOCAL_PATH = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]|/(?:Users|home)/")


def local_paths_in(manifest) -> list:
    """Manifest fields whose text holds a path from the building machine.

    The manifest ships inside a public release and is copied into the release
    notes, so a home folder or a drive path in it publishes the builder's
    account name. Returns the dotted names of the offending fields; the file
    list is relative to the package by construction and is not scanned.
    """
    home = str(Path.home())
    marks = (home, home.replace("\\", "/"))

    def scan(value, name, found):
        if isinstance(value, dict):
            for key, inner in value.items():
                scan(inner, "%s.%s" % (name, key), found)
        elif isinstance(value, (list, tuple)):
            for index, inner in enumerate(value):
                scan(inner, "%s[%d]" % (name, index), found)
        elif isinstance(value, str):
            if any(mark in value for mark in marks) or _LOCAL_PATH.search(value):
                found.append(name)

    found = []
    for key, value in (manifest or {}).items():
        if key != "files":
            scan(value, str(key), found)
    return found


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


def verify_zip(zip_path, manifest=None, counts=None):
    """Re-open a finished ZIP and check it against its manifest.

    Guards against files vanishing from the staging tree while the build runs
    (an over-eager real-time scanner), which otherwise produces an archive that
    is quietly short of what the build reported writing. Returns a list of
    problems; an empty list means the archive is internally consistent.
    """
    zip_path = Path(zip_path)
    problems = []
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        if manifest is None:
            try:
                manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
            except KeyError:
                return ["ZIP has no %s" % MANIFEST_NAME]
            except ValueError as error:
                return ["%s is not valid JSON: %s" % (MANIFEST_NAME, error)]

        listed = [str(entry.get("path", "")) for entry in manifest.get("files") or []]
        expected = set(listed)
        actual = names - {MANIFEST_NAME}
        if len(listed) != len(expected):
            problems.append("the manifest lists a path more than once")
        missing = sorted(expected - actual)
        if missing:
            problems.append("%d manifest file(s) missing from the ZIP, e.g. %s"
                            % (len(missing), ", ".join(missing[:3])))
        extra = sorted(actual - expected)
        if extra:
            problems.append("%d ZIP file(s) absent from the manifest, e.g. %s"
                            % (len(extra), ", ".join(extra[:3])))

        counts = counts if counts is not None else (manifest.get("counts") or {})
        if counts:
            ad = sum(1 for name in actual if name.startswith("CONFIG/ad/"))
            want_ad = (counts.get("layouts") or 0) + (counts.get("layouts_custom") or 0)
            if want_ad and ad < want_ad:
                problems.append("%d CONFIG/ad layout(s) present, build counted %d"
                                % (ad, want_ad))
            draw = sum(1 for name in actual if name.endswith(".draw"))
            want_draw = (counts.get("enr_airspace") or 0) + (counts.get("ad_airspace") or 0)
            if want_draw and draw < want_draw:
                problems.append("%d .draw drawing(s) present, build counted %d"
                                % (draw, want_draw))
    return problems


def asset_name(region: str, airac_code: str) -> str:
    """Release asset name, e.g. ``EG_2610.zip`` (FIR + AIRAC code).

    Every cycle gets its own asset name. SkyRadar resolves the newest asset from
    the repository's latest release rather than a fixed URL.
    """
    return "%s_%s.zip" % (region.upper(), airac_code)


# --- Release notes ----------------------------------------------------------

_COUNT_LABELS = {
    "aerodromes": "Aerodromes",
    "navaids": "Navaids",
    "fixes": "Fixes",
    "airway_segments": "Airway segments",
    "entry_exit_points": "SID/STAR entry-exit points",
    "enr_airspace": "ENR 2.1 sector drawings",
    "ad_airspace": "Local airspace drawings",
    "ctr_positions": "Default CTR positions",
    "layouts": "X-Plane Gateway layouts",
    "layouts_custom": "Local layout overrides",
    "layouts_unavailable": "Layouts unavailable",
    "layouts_failed": "Layouts failed",
    "base_vfr_points": "openAIP VFR reporting points",
    "base_navaids": "openAIP navaids added",
    "base_aerodromes": "Base aerodrome positions added",
    "base_rows_navaids": "Base rows at the end of navaid.dat",
    "base_rows_aerodromes": "Base rows at the end of AD-positions",
    "airspace_drawings": "openAIP airspace drawings",
    "overrides": "Local CONFIG overrides",
}


def _count_label(key: str) -> str:
    return _COUNT_LABELS.get(key, key.replace("_", " ").capitalize())


def render_release_notes(manifest) -> str:
    """Markdown release notes for a build, ready to paste into a GitHub release."""
    region = str(manifest.get("region") or REGION)
    airac = str(manifest.get("airac") or "")
    code = str(manifest.get("airac_code") or "")
    asset = asset_name(region, code)
    files = manifest.get("files") or []
    counts = manifest.get("counts") or {}
    sources = manifest.get("sources") or {}
    overrides = manifest.get("overrides") or []

    lines = [
        "# AIRAC %s - %s navdata" % (airac or code, region),
        "",
        "SkyRadar navigation data for the **%s** FIR/UIR." % region,
        "",
        "Install with SkyRadar's launcher -> **AIRAC update**, or download `%s` and "
        "extract it over the SkyRadar folder." % asset,
        "",
        "## Asset",
        "",
        "| | |",
        "|---|---|",
        "| File | `%s` |" % asset,
        "| AIRAC cycle | %s (`%s`) |" % (airac, code),
        "| Region | %s |" % region,
        "| Files | %d |" % len(files),
        "| Built | %s |" % manifest.get("built_utc", ""),
        "| Generator | %s %s |" % (manifest.get("generator", GENERATOR_NAME),
                                   manifest.get("generator_version", "")),
        "",
    ]

    if counts:
        lines += ["## Counts", "", "| Metric | Value |", "|---|---|"]
        for key in sorted(counts):
            lines.append("| %s | %s |" % (_count_label(key), counts[key]))
        lines.append("")

    lines += [
        "## Contents",
        "",
        "- `CONFIG/nav/` - navaids, fixes, airways, aerodrome positions, "
        "SID/STAR entry-exit points and default enroute positions",
        "- `CONFIG/ad/<ICAO>.dat` - airport layouts",
        "- `CONFIG/bg/` - airspace drawings (`ENR21.lst`, per-aerodrome `.lst` "
        "and `.draw`)",
        "- `manifest.json` - SHA-256 and size for every file above",
        "",
    ]

    if overrides:
        lines += ["## Local overrides", "",
                  "Taken from the repository's `CONFIG/` folder; these replace the "
                  "generated versions:", ""]
        lines += ["- `%s`" % path for path in overrides]
        lines.append("")

    if sources:
        lines += ["## Sources", ""]
        for key in sorted(sources):
            lines.append("- **%s:** %s" % (key, sources[key]))
        lines.append("")

    lines += [
        "SkyRadar installs only `CONFIG/nav/`, `CONFIG/ad/` and `CONFIG/bg/`, verifies "
        "each file's SHA-256 and size, prunes files from the previous cycle, and never "
        "touches `route-presets` or `CONFIG/bg/Notice`.",
        "",
    ]
    return "\n".join(lines)


def write_release_notes(path, manifest) -> Path:
    """Write the markdown release notes for ``manifest`` to ``path``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_release_notes(manifest), encoding="utf-8")
    return path
