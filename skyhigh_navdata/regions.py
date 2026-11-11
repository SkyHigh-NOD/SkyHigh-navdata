"""Region and world release packaging.

Alongside the combined ``EG_<airac>.zip`` (which SkyRadar consumes, and which
doubles as the *EG region* package), the build emits:

- one ZIP per **ICAO region** — the code's leading letters, e.g. ``LF`` for
  France, ``ED`` for Germany, ``K`` for the USA — bounding-box-filtered from
  the finished tree, so a pilot flying one region installs a small package;
- one **world ZIP** (``ALL_<airac>.zip``) with the fully merged global tree,
  which skymaps consumes as its navdata base.

Region grouping uses the ICAO two-letter country prefix, except for the
regions that are a single letter in practice (``K`` USA, ``C`` Canada,
``Y`` Australia), where the first letter is the region.

Every subset is a bounding box and deliberately errs on the generous side;
point data (fixes, navaids, aerodromes, entry/exit points, airway segments,
airport layouts) is filtered per record.

Airspace drawings come from the openAIP exports: the eAIP-generated airspace
is currently broken, so openAIP *replaces* it entirely. If the eAIP airspace
parser is ever fixed, this is the place to layer SkyHigh data back on top.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from . import REGION, pack
from .airspace import airspace_pretty, airspace_slug, write_draw_sections, write_lst_file

# The combined build's airspace scope: everything the EG navdata covers.
EG_REGION = {
    "name": "EG (UK + Ireland)",
    "bbox": (48.8, -17.5, 61.5, 2.2),
    "countries": ("GB", "IE"),
}

# Everything, for worldwide airspace packaging.
WORLD_BBOX = (-90.0, -180.0, 90.0, 180.0)

# Regions whose ICAO codes are region + three letters (not two-letter country
# prefixes); everything else groups by the first two letters.
SINGLE_LETTER_REGIONS = {"K", "C", "Y"}

AD_POSITION_RE = re.compile(r"^([A-Z0-9]{4}) (-?\d+\.\d+),(-?\d+\.\d+)")


def in_bbox(bbox, lat: float, lon: float) -> bool:
    lat_min, lon_min, lat_max, lon_max = bbox
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def region_prefix(icao: str) -> str:
    """ICAO code → its region prefix ('EGKK' → 'EG', 'KJFK' → 'K')."""
    return icao[:1] if icao[0] in SINGLE_LETTER_REGIONS else icao[:2]


def resolve_regions(spec: str | None, stage_dir: Path, min_airports: int) -> list:
    """Region list to build: 'auto' (default) derives every prefix from the
    staged AD-positions with at least ``min_airports`` aerodromes; a comma
    list of prefixes builds exactly those; 'none' builds no region ZIPs.
    The combined region (EG) is never re-emitted — the combined ZIP is it."""
    if spec is None or spec.strip().lower() in ("auto", "all", "*", ""):
        counts: dict = {}
        for icao in _staged_region_airports(stage_dir):
            counts[region_prefix(icao)] = counts.get(region_prefix(icao), 0) + 1
        return [p for p, n in sorted(counts.items())
                if n >= min_airports and p != REGION]
    spec = spec.strip().lower()
    if spec == "none":
        return []
    chosen = []
    for token in spec.split(","):
        prefix = token.strip().upper()
        if not prefix:
            continue
        if prefix == REGION:
            continue  # the combined ZIP is the EG region package
        chosen.append(prefix)
    return chosen


def _staged_region_airports(stage_dir: Path) -> list:
    path = Path(stage_dir) / "CONFIG" / "nav" / "AD-positions"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = AD_POSITION_RE.match(line)
        if match:
            out.append(match.group(1))
    return out


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


def _filter_ad_positions(source: Path, target: Path, bbox, prefix=None) -> tuple[int, dict]:
    """Returns (kept, {icao: (lat, lon)}) — by bounding box, or by region
    prefix when one is given."""
    kept = 0
    icaos = {}
    lines = []
    for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
        match = AD_POSITION_RE.match(line)
        if not match:
            continue  # drops the trailing count line; rewritten below
        icao, lat, lon = match.group(1), float(match.group(2)), float(match.group(3))
        if prefix is not None:
            if region_prefix(icao) != prefix:
                continue
        elif not in_bbox(bbox, lat, lon):
            continue
        icaos[icao] = (lat, lon)
        lines.append(line.rstrip())
        kept += 1
    if lines:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines + [str(len(lines))]) + "\n", encoding="utf-8")
    return kept, icaos


def _filter_airways(source: Path, target: Path, fix_points: dict) -> int:
    """Keep segments whose two endpoints are both inside the fix set."""
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


def _filter_entry_exit(source: Path, target: Path, region_icaos: dict) -> int:
    kept = 0
    lines = []
    for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("#") or not line.strip():
            lines.append(line)
            continue
        airport = line.split()[0].upper() if line.split() else ""
        if airport not in region_icaos:
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


def write_world_airspace_groups(bg_dir: Path, airspaces: list, airac_cycle: str) -> int:
    """Every openAIP airspace, bucketed into per-country .lst groups
    (``OAIP<ISO>`` — letters/digits only, as SkyRadar's launcher requires).

    Returns the total drawing count. Countries with no drawing are skipped.
    """
    by_country: dict = {}
    for airspace in airspaces:
        by_country.setdefault(airspace.get("country") or "XX", []).append(airspace)
    total = 0
    for country in sorted(by_country):
        added, _group = write_openaip_airspace_group(
            bg_dir, by_country[country], WORLD_BBOX, airac_cycle,
            group="OAIP" + country)
        total += added
    return total


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
        # Bounding-box intersection (cached per airspace) instead of a
        # point-by-point scan — this runs once per region per build.
        airspace_bbox = airspace.get("_bbox", "")
        if airspace_bbox == "":
            points = [pt for ring in airspace.get("rings", []) for pt in ring]
            if points:
                airspace_bbox = (
                    min(p[0] for p in points), min(p[1] for p in points),
                    max(p[0] for p in points), max(p[1] for p in points),
                )
            airspace["_bbox"] = airspace_bbox
        if not airspace_bbox:
            continue
        if not (airspace_bbox[0] <= bbox[2] and bbox[0] <= airspace_bbox[2]
                and airspace_bbox[1] <= bbox[3] and bbox[1] <= airspace_bbox[3]):
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


# --- The world ZIP (skymaps) -------------------------------------------------

def build_global_zip(stage_dir: Path, out_dir: Path, airac_display: str,
                     airac_code: str, generator_version="",
                     notes_out=None, log=None) -> Path:
    """Package the fully merged staging tree as ALL_<airac>.zip.

    The tree is exactly the combined build's content (global nav base merged
    under the SkyHigh data, EG-scope airspace drawings) — re-manifested with
    region ALL so consumers can tell it apart from the SkyRadar EG ZIP.
    """
    def _log(message):
        if log:
            log(message)

    manifest = pack.build_manifest(
        stage_dir, region="ALL", airac=airac_display, airac_code=airac_code,
        generator_version=generator_version,
    )
    pack.write_manifest(stage_dir, manifest)
    zip_path = pack.make_zip(stage_dir, Path(out_dir) / pack.asset_name("ALL", airac_code))
    problems = pack.verify_zip(zip_path, manifest)
    if problems:
        raise RuntimeError("World ZIP failed verification: %s" % "; ".join(problems))
    if notes_out is None:
        notes_out = zip_path.with_suffix(".md")
    pack.write_release_notes(notes_out, manifest)
    _log("world: %d file(s) -> %s" % (len(manifest["files"]), zip_path.name))
    return zip_path


# --- One region ZIP ----------------------------------------------------------

def build_region_zip(prefix: str, stage_dir: Path, out_dir: Path,
                     airac_display: str, airac_code: str, base_airspaces=None,
                     generator_version="", log=None) -> Path | None:
    """Package one ICAO region's subset of the staged tree as <prefix>_<airac>.zip."""
    def _log(message):
        if log:
            log(message)

    nav_source = Path(stage_dir) / "CONFIG" / "nav"
    positions_source = nav_source / "AD-positions"
    if not positions_source.is_file():
        return None

    # region aerodromes drive both the membership test and the bounding box
    region_icaos = {}
    for line in positions_source.read_text(encoding="utf-8", errors="replace").splitlines():
        match = AD_POSITION_RE.match(line)
        if not match:
            continue
        icao, lat, lon = match.group(1), float(match.group(2)), float(match.group(3))
        if region_prefix(icao) == prefix:
            region_icaos[icao] = (lat, lon)
    if not region_icaos:
        return None
    lats = [lat for lat, _ in region_icaos.values()]
    lons = [lon for _, lon in region_icaos.values()]
    bbox = (min(lats) - 2.0, min(lons) - 4.0, max(lats) + 2.0, max(lons) + 4.0)

    region_stage = Path(out_dir) / (".staging-%s" % prefix)
    if region_stage.exists():
        shutil.rmtree(region_stage)
    region_stage.mkdir(parents=True)
    (region_stage / "CONFIG").mkdir()

    counts = {}
    nav_target = region_stage / "CONFIG" / "nav"
    if (nav_source / "fix.dat").is_file():
        counts["fixes"] = _filter_fix_file(nav_source / "fix.dat", nav_target / "fix.dat", bbox)
    filtered_fixes = _parse_fix_points(nav_target / "fix.dat") if (nav_target / "fix.dat").is_file() else {}
    if (nav_source / "navaid.dat").is_file():
        counts["navaids"] = _filter_navaid_file(nav_source / "navaid.dat", nav_target / "navaid.dat", bbox)
    _filter_ad_positions(positions_source, nav_target / "AD-positions", bbox, prefix=prefix)
    counts["aerodromes"] = len(region_icaos)
    if (nav_source / "awy.dat").is_file():
        counts["airway_segments"] = _filter_airways(nav_source / "awy.dat", nav_target / "awy.dat", filtered_fixes)
    if (nav_source / "AD-entry-exit").is_file():
        counts["entry_exit_points"] = _filter_entry_exit(nav_source / "AD-entry-exit", nav_target / "AD-entry-exit", region_icaos)
    # airac.txt is regenerated per region (the staged one says region: EG)
    (nav_target / "airac.txt").parent.mkdir(parents=True, exist_ok=True)
    (nav_target / "airac.txt").write_text(
        "region: %s\nairac: %s\nairac_code: %s\nsubset: filtered by ICAO region prefix\n"
        % (prefix, airac_display, airac_code),
        encoding="utf-8")

    ad_source = Path(stage_dir) / "CONFIG" / "ad"
    if ad_source.is_dir():
        ad_target = region_stage / "CONFIG" / "ad"
        ad_target.mkdir(parents=True, exist_ok=True)
        layouts = 0
        for icao in sorted(region_icaos):
            layout = ad_source / ("%s.dat" % icao)
            if layout.is_file():
                shutil.copyfile(layout, ad_target / layout.name)
                layouts += 1
        counts["layouts"] = layouts

    if base_airspaces:
        added, _group = write_openaip_airspace_group(
            region_stage / "CONFIG" / "bg", base_airspaces, bbox, airac_display)
        counts["openaip_airspaces"] = added

    manifest = pack.build_manifest(
        region_stage, region=prefix, airac=airac_display, airac_code=airac_code,
        sources={"subset": "filtered from the combined build by ICAO region prefix"},
        counts=counts, generator_version=generator_version,
    )
    pack.write_manifest(region_stage, manifest)
    zip_path = pack.make_zip(region_stage, Path(out_dir) / pack.asset_name(prefix, airac_code))
    problems = pack.verify_zip(zip_path, manifest)
    if problems:
        raise RuntimeError("Region %s ZIP failed verification: %s" % (prefix, "; ".join(problems)))
    _log("region %s: %d file(s) -> %s" % (prefix, len(manifest["files"]), zip_path.name))
    shutil.rmtree(region_stage, ignore_errors=True)
    return zip_path
