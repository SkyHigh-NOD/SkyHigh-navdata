"""Per-FIR release packaging.

Besides the combined ``EG_<airac>.zip`` (which SkyRadar and skymaps consume),
the build emits one ZIP per FIR containing only the data inside that FIR's
bounds — so a pilot or controller flying only one FIR can install a smaller
package.

FIR selection is a bounding box; it deliberately errs on the generous side
(FIR boundaries are polygons, and a rectangle over-covers rather than
under-covers). Point data (fixes, navaids, aerodromes, entry/exit points,
airway segments, airport layouts) is filtered per record.

Airspace drawings come from the openAIP exports: the eAIP-generated airspace
is currently broken, so openAIP *replaces* it entirely (per-FIR by bounding
box, plus a per-FIR country allow-list where the rectangle over-catches
neighbours). If the eAIP airspace parser is ever fixed, this is the place to
layer SkyHigh data back on top.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from . import GENERATOR_NAME, pack
from .airspace import airspace_pretty, airspace_slug, write_draw_sections, write_lst_file

# The combined build's airspace scope: everything the EG navdata covers.
EG_REGION = {
    "name": "EG (UK + Ireland)",
    "bbox": (48.8, -17.5, 61.5, 2.2),
    "countries": ("GB", "IE"),
}

# FIR id → {name, bbox: (lat_min, lon_min, lat_max, lon_max), countries?}
# Add further FIRs here as the network's coverage grows. `countries` (ISO
# codes) optionally restricts which countries' openAIP airspaces may fill
# the FIR's gaps — bounding boxes are rectangles, FIRs are not, so this
# trims over-catch from neighbouring countries.
FIRS = {
    "EGTT": {"name": "London FIR", "bbox": (49.8, -6.7, 55.9, 2.2)},
    "EGPX": {"name": "Scottish FIR", "bbox": (54.3, -9.5, 61.5, 0.3)},
    "EGGX": {"name": "Shanwick Oceanic FIR", "bbox": (43.5, -17.5, 61.0, -0.8),
             "countries": ("GB", "IE")},
    "EISN": {"name": "Shannon FIR", "bbox": (48.8, -13.1, 55.5, -5.3),
             "countries": ("IE", "GB")},
}

ICAO_RE = re.compile(r"^[A-Z0-9]{4}$")
AD_POSITION_RE = re.compile(r"^([A-Z0-9]{4}) (-?\d+\.\d+),(-?\d+\.\d+)")


def in_bbox(bbox, lat: float, lon: float) -> bool:
    lat_min, lon_min, lat_max, lon_max = bbox
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def resolve_firs(spec: str | None) -> dict:
    """'EGTT,EISN' / 'all' / None → the FIR subset to build."""
    if spec is None:
        return FIRS
    spec = spec.strip().lower()
    if not spec or spec in ("all", "*"):
        return FIRS
    chosen = {}
    for token in spec.split(","):
        key = token.strip().upper()
        if not key:
            continue
        if key not in FIRS:
            raise SystemExit("Unknown FIR %r; known: %s" % (key, ", ".join(FIRS)))
        chosen[key] = FIRS[key]
    return chosen


# --- Filtering helpers -------------------------------------------------------

def _parse_fix_points(path: Path) -> dict:
    """fix.dat → {name: (lat, lon)}"""
    points = {}
    if not path.is_file():
        return points
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 3:
            try:
                points[parts[2].upper()] = (float(parts[0]), float(parts[1]))
            except ValueError:
                continue
    return points


def _filter_fix_file(source: Path, target: Path, bbox) -> int:
    kept = 0
    lines = []
    for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 3:
            try:
                if not in_bbox(bbox, float(parts[0]), float(parts[1])):
                    continue
            except ValueError:
                pass
        lines.append(line)
        kept += 1
    if lines:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return kept


def _filter_navaid_file(source: Path, target: Path, bbox) -> int:
    kept = 0
    lines = []
    for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 11:
            try:
                if not in_bbox(bbox, float(parts[1]), float(parts[2])):
                    continue
            except ValueError:
                pass
        lines.append(line)
        if len(parts) >= 11:
            kept += 1
    if lines:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return kept


def _filter_ad_positions(source: Path, target: Path, bbox) -> tuple[int, dict]:
    """Returns (kept, {icao: (lat, lon)}) for the FIR."""
    kept = 0
    icaos = {}
    lines = []
    for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
        match = AD_POSITION_RE.match(line)
        if not match:
            continue  # drops the trailing count line; rewritten below
        lat, lon = float(match.group(2)), float(match.group(3))
        if not in_bbox(bbox, lat, lon):
            continue
        icaos[match.group(1)] = (lat, lon)
        lines.append(line.rstrip())
        kept += 1
    if lines:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines + [str(len(lines))]) + "\n", encoding="utf-8")
    return kept, icaos


def _filter_airways(source: Path, target: Path, fix_points: dict) -> int:
    """Keep segments whose two endpoints are both inside the FIR's fix set."""
    kept = 0
    lines = []
    for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 11:
            start, end = parts[0].upper(), parts[3].upper()
            if start not in fix_points or end not in fix_points:
                continue
        lines.append(line)
        if len(parts) >= 11:
            kept += 1
    if lines:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return kept


def _filter_entry_exit(source: Path, target: Path, fir_icaos: dict) -> int:
    kept = 0
    lines = []
    for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("#") or not line.strip():
            lines.append(line)
            continue
        airport = line.split()[0].upper() if line.split() else ""
        if airport not in fir_icaos:
            continue
        lines.append(line)
        kept += 1
    if lines:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return kept


def _normalise_airspace_name(name: str) -> list:
    """Uppercase, punctuation ('/', '-') as spaces, collapsed tokens."""
    return [t for t in re.sub(r"[^A-Z0-9]+", " ", name.upper()).split() if t]


def _family_keys(name: str) -> set:
    """Dedup keys for an airspace name: its 1- and 2-token prefixes."""
    tokens = _normalise_airspace_name(name)
    keys = set()
    for width in (1, 2):
        if len(tokens) >= width:
            keys.add(" ".join(tokens[:width]))
    return keys


def write_openaip_airspace_group(bg_dir: Path, airspaces: list, bbox, airac_cycle: str,
                                 countries=None, group: str = "OAIP",
                                 dedupe_families=None) -> tuple[int, str]:
    """Write airspaces intersecting the bbox as CONFIG/bg/<group>/.draw files
    with a <group>.lst index. Returns (count, group name).

    `countries` optionally restricts the airspaces' ISO country codes.
    `dedupe_families` optionally skips names whose family keys are already
    present (used when layering a source *under* a better one).
    """
    group_dir = bg_dir / group
    index = []
    count = 0
    seen_stems = set()
    for airspace in airspaces:
        if countries and airspace.get("country") not in countries:
            continue
        if dedupe_families and (_family_keys(airspace["name"]) & dedupe_families):
            continue
        points = [pt for ring in airspace.get("rings", []) for pt in ring]
        if not any(in_bbox(bbox, lat, lon) for lat, lon in points):
            continue
        stem = airspace_slug(airspace["name"])
        if stem in seen_stems:
            continue
        seen_stems.add(stem)
        filename = "%s.draw" % stem
        header = [airspace["name"]]
        if airspace.get("upper"):
            header.append("Upper limit: %s" % airspace["upper"])
        if airspace.get("lower"):
            header.append("Lower limit: %s" % airspace["lower"])
        header.append("Source: openAIP daily export, cycle %s" % airac_cycle)
        # one section per ring keeps MultiPolygons (several volumes) visible
        sections = [(header, ring) for ring in airspace.get("rings", [])]
        write_draw_sections(group_dir / filename, sections)
        index.append("%s/%s  DRAW  %s" % (group, filename, airspace_pretty(airspace["name"].split())))
        count += 1
    if count:
        write_lst_file(bg_dir / ("%s.lst" % group), index)
    return count, group


# --- One FIR release ---------------------------------------------------------

def build_fir_zip(fir_id: str, fir: dict, stage_dir: Path, out_dir: Path,
                  airac_display: str, airac_code: str, base_airspaces=None,
                  generator_version="",
                  notes_out=None, log=None):
    """Package one FIR's subset of the staged tree as <FIR>_<airac>.zip."""
    def _log(message):
        if log:
            log(message)

    bbox = fir["bbox"]
    fir_stage = Path(out_dir) / (".staging-%s" % fir_id)
    if fir_stage.exists():
        shutil.rmtree(fir_stage)
    fir_stage.mkdir(parents=True)
    (fir_stage / "CONFIG").mkdir()

    counts = {}

    # nav files
    nav_source = stage_dir / "CONFIG" / "nav"
    nav_target = fir_stage / "CONFIG" / "nav"
    fix_points = _parse_fix_points(nav_source / "fix.dat")
    filtered_fixes = {}
    if (nav_source / "fix.dat").is_file():
        counts["fixes"] = _filter_fix_file(nav_source / "fix.dat", nav_target / "fix.dat", bbox)
        # FIR-local fix set = the names kept in the filtered file
        filtered_fixes = _parse_fix_points(nav_target / "fix.dat")
    if (nav_source / "navaid.dat").is_file():
        counts["navaids"] = _filter_navaid_file(nav_source / "navaid.dat", nav_target / "navaid.dat", bbox)
    if (nav_source / "AD-positions").is_file():
        kept, fir_icaos = _filter_ad_positions(nav_source / "AD-positions", nav_target / "AD-positions", bbox)
        counts["aerodromes"] = kept
    else:
        fir_icaos = {}
    if (nav_source / "awy.dat").is_file():
        counts["airway_segments"] = _filter_airways(nav_source / "awy.dat", nav_target / "awy.dat", filtered_fixes or fix_points)
    if (nav_source / "AD-entry-exit").is_file():
        counts["entry_exit_points"] = _filter_entry_exit(nav_source / "AD-entry-exit", nav_target / "AD-entry-exit", fir_icaos)
    for name in ("CTR-positions", "airac.txt"):
        source = nav_source / name
        if source.is_file():
            (nav_target / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, nav_target / name)

    # airport layouts
    ad_source = stage_dir / "CONFIG" / "ad"
    ad_target = fir_stage / "CONFIG" / "ad"
    if ad_source.is_dir():
        ad_target.mkdir(parents=True, exist_ok=True)
        layouts = 0
        for icao in sorted(fir_icaos):
            layout = ad_source / ("%s.dat" % icao)
            if layout.is_file():
                shutil.copyfile(layout, ad_target / layout.name)
                layouts += 1
        counts["layouts"] = layouts

    # airspace drawings: openAIP replaces the broken eAIP-generated ones
    bg_target = fir_stage / "CONFIG" / "bg"
    if base_airspaces:
        added, _group = write_openaip_airspace_group(
            bg_target, base_airspaces, bbox, airac_display,
            countries=fir.get("countries"))
        counts["openaip_airspaces"] = added

    if not any((fir_stage / "CONFIG").glob("*/*")):
        _log("FIR %s: empty subset, skipping ZIP" % fir_id)
        shutil.rmtree(fir_stage, ignore_errors=True)
        return None

    manifest = pack.build_manifest(
        fir_stage, region=fir_id, airac=airac_display, airac_code=airac_code,
        sources={"subset": "filtered from the combined build by FIR bounding box"},
        counts=counts, generator_version=generator_version,
    )
    pack.write_manifest(fir_stage, manifest)

    zip_path = pack.make_zip(fir_stage, Path(out_dir) / pack.asset_name(fir_id, airac_code))
    problems = pack.verify_zip(zip_path, manifest)
    if problems:
        raise RuntimeError("FIR %s ZIP failed verification: %s" % (fir_id, "; ".join(problems)))
    if notes_out is None:
        notes_out = zip_path.with_suffix(".md")
    pack.write_release_notes(notes_out, manifest)
    _log("FIR %s: %d file(s) -> %s" % (fir_id, len(manifest["files"]), zip_path.name))
    shutil.rmtree(fir_stage, ignore_errors=True)
    return zip_path
