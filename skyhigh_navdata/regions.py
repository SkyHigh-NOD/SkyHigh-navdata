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

Airspace comes from two sources and a default build ships both, because the
two readers want different ones: skymaps' chart build reads the eAIP drawings
(``CONFIG/bg/ENR21``), and SkyRadar reads the openAIP per-country groups
(``CONFIG/bg/OAIP<ISO>.lst``) with ``CONFIG/bg/OAIP-countries`` to pick the
group for a position. ``--airspace-source`` narrows that to one of them, and
the choice applies to every ZIP the build writes. The eAIP covers the UK
only, so a region ZIP elsewhere carries openAIP drawings or none.
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
ICAO_RE = re.compile(r"^[A-Z]{4}$")


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
            # A local ident such as 00AK is not an ICAO code and has no region.
            if not ICAO_RE.match(icao):
                continue
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


def _parse_navaid_idents(path: Path) -> set:
    """navaid.dat -> the idents in it."""
    idents = set()
    if not path.is_file():
        return idents
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 11:
            idents.add(parts[7].upper())
    return idents


def _filter_vfr_points(source: Path, target: Path, bbox) -> int:
    """vfr-points rows (tab-separated lat, lon, country, name) inside the box."""
    kept = 0
    lines = []
    for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("#") or not line.strip():
            lines.append(line)
            continue
        parts = line.split("\t")
        try:
            if not in_bbox(bbox, float(parts[0]), float(parts[1])):
                continue
        except (IndexError, ValueError):
            continue
        lines.append(line)
        kept += 1
    if kept:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return kept


def _filter_airways(source: Path, target: Path, fix_points) -> int:
    """Keep segments whose two endpoints are both retained in the region.

    ``fix_points`` is any container of endpoint names: the region's fixes and
    its navaids, since an airway can start or end on either.
    """
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


def write_country_airspace_groups(bg_dir: Path, airspaces: list, bbox, airac_cycle: str,
                                  countries=None) -> int:
    """openAIP airspace touching ``bbox``, bucketed into per-country .lst groups
    (``OAIP<ISO>`` — letters/digits only, as SkyRadar's launcher requires).

    This is the layout SkyRadar reads. ``countries`` optionally restricts the
    ISO codes. Returns the total drawing count; a country with no drawing in
    the box gets no group.
    """
    by_country: dict = {}
    for airspace in airspaces:
        country = str(airspace.get("country") or "").upper()
        if not re.match(r"^[A-Z]{2}$", country):
            country = "XX"
        if countries and country not in countries:
            continue
        by_country.setdefault(country, []).append(airspace)
    total = 0
    for country in sorted(by_country):
        added, _group = write_openaip_airspace_group(
            bg_dir, by_country[country], bbox, airac_cycle, group="OAIP" + country)
        total += added
    return total


def write_world_airspace_groups(bg_dir: Path, airspaces: list, airac_cycle: str) -> int:
    """Every openAIP airspace as per-country groups, replacing any earlier ones."""
    # Every earlier openAIP group goes first: the flat one, and the
    # per-country ones, so a country that no longer has drawings keeps none.
    clear_openaip_groups(bg_dir)
    return write_country_airspace_groups(bg_dir, airspaces, WORLD_BBOX, airac_cycle)


def write_openaip_airspace(bg_dir: Path, base_data: dict, scope: str, airac_cycle: str,
                           log=None) -> int:
    """Draw the openAIP airspace for a combined build, replacing earlier groups.

    ``scope`` is ``world`` (every country) or ``eg`` (the UK and Ireland
    only). Both write per-country ``OAIP<ISO>`` groups and the
    ``OAIP-countries`` map, which is what SkyRadar reads.
    """
    bg_dir = Path(bg_dir)
    clear_openaip_groups(bg_dir)
    airspaces = base_data.get("airspaces") or []
    if scope == "eg":
        added = write_country_airspace_groups(
            bg_dir, airspaces, EG_REGION["bbox"], airac_cycle,
            countries=EG_REGION["countries"])
    else:
        added = write_country_airspace_groups(bg_dir, airspaces, WORLD_BBOX, airac_cycle)
    mapped = 0
    if added:
        mapped = write_icao_country_map(
            bg_dir, base_data.get("ourairports"), base_data.get("airports"))
    if log:
        log("airspace: %d openAIP drawing(s) in per-country groups (%s scope), "
            "%d ICAO prefix(es) mapped" % (added, scope, mapped))
    return added


def clear_openaip_groups(bg_dir: Path) -> int:
    """Remove every openAIP airspace group from ``CONFIG/bg``.

    That is ``OAIP.lst`` and ``OAIP/`` (the flat UK and Ireland group), each
    ``OAIP<ISO>.lst`` and ``OAIP<ISO>/`` (the per-country groups) and the
    ``OAIP-countries`` map. Returns how many entries were removed.
    """
    bg_dir = Path(bg_dir)
    if not bg_dir.is_dir():
        return 0
    removed = 0
    for entry in sorted(bg_dir.iterdir()):
        if not re.match(r"^OAIP([A-Z]{2})?(\.lst)?$|^OAIP-countries$", entry.name):
            continue
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink()
        removed += 1
    return removed


def write_icao_country_map(bg_dir: Path, ourairports: dict, openaip_airports=None) -> int:
    """Write ``CONFIG/bg/OAIP-countries``: one 'ICAO-prefix ISO' per line.

    SkyRadar reads it to pick the ``OAIP<ISO>.lst`` airspace group matching a
    position's ICAO country prefix (e.g. EG aerodromes -> the GB group). The
    mapping is a majority vote of the aerodromes' countries per prefix, taken
    over OurAirports and the openAIP airports together (an aerodrome in both
    votes once, with openAIP's country), so either source alone still gives a
    map. Returns the number of prefixes written.
    """
    from collections import Counter
    # Takes what load_ourairports returns ({"airports": {...}, "counts": {...}})
    # or the aerodrome dict itself.
    airports = ourairports or {}
    if isinstance(airports.get("airports"), dict):
        airports = airports["airports"]
    airports = dict(airports)
    airports.update(openaip_airports or {})
    by_prefix: dict = {}
    for icao, record in airports.items():
        if not isinstance(icao, str) or not ICAO_RE.match(icao) or not isinstance(record, dict):
            continue
        country = str(record.get("country") or "").upper()
        if not re.match(r"^[A-Z]{2}$", country):
            continue
        by_prefix.setdefault(region_prefix(icao), Counter())[country] += 1
    lines = []
    for prefix in sorted(by_prefix):
        iso, _votes = by_prefix[prefix].most_common(1)[0]
        lines.append("%s %s" % (prefix, iso))
    if not lines:
        return 0
    out = Path(bg_dir) / "OAIP-countries"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "# ICAO region prefix -> openAIP ISO country (majority of aerodromes).\n"
        "# Read by SkyRadar to pick the OAIP<ISO>.lst airspace group for a\n"
        "# position's country.\n" + "\n".join(lines) + "\n",
        encoding="utf-8")
    return len(lines)


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
    # slug -> the volumes already written under it (limits and rings)
    seen_stems: dict = {}
    used_stems: set = set()
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
        # Two volumes can share a name (a CTA in several parts, or the same
        # name in two places). An exact repeat is dropped; a different volume
        # gets a file of its own.
        slug = airspace_slug(airspace["name"])
        volume = (airspace.get("upper"), airspace.get("lower"), airspace.get("rings"))
        written = seen_stems.setdefault(slug, [])
        if volume in written:
            continue
        written.append(volume)
        stem, serial = slug, 1
        while stem in used_stems:
            serial += 1
            stem = "%s-%d" % (slug, serial)
        used_stems.add(stem)
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


DRAW_POINT_RE = re.compile(r"^(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)$")
OPENAIP_INDEX_RE = re.compile(r"^OAIP(?:[A-Z]{2})?\.lst$")


def _draw_file_bbox(path: Path):
    """(lat_min, lon_min, lat_max, lon_max) of a .draw file's points, or None."""
    lats, lons = [], []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = DRAW_POINT_RE.match(line.strip())
        if match:
            lats.append(float(match.group(1)))
            lons.append(float(match.group(2)))
    if not lats:
        return None
    return (min(lats), min(lons), max(lats), max(lons))


def copy_staged_airspace_groups(stage_bg: Path, region_bg: Path, bbox) -> int:
    """Copy the staged openAIP drawings touching ``bbox`` into a region tree.

    The files are taken as they are in ``stage_bg`` (``OAIP<ISO>.lst`` groups,
    or the flat ``OAIP.lst``), never redrawn, so a region package made from a
    published ZIP holds exactly the drawings that ZIP has. Returns how many
    were copied; a group with no drawing in the box is not written.
    """
    stage_bg, region_bg = Path(stage_bg), Path(region_bg)
    if not stage_bg.is_dir():
        return 0
    total = 0
    for index_path in sorted(stage_bg.iterdir()):
        if not index_path.is_file() or not OPENAIP_INDEX_RE.match(index_path.name):
            continue
        kept = []
        for entry in index_path.read_text(encoding="utf-8", errors="replace").splitlines():
            relative = entry.split("  DRAW  ", 1)[0].strip()
            if not relative or not pack.allowed_relative_path("CONFIG/bg/" + relative):
                continue
            source = stage_bg / relative
            if not source.is_file():
                continue
            box = _draw_file_bbox(source)
            if not box or not (box[0] <= bbox[2] and bbox[0] <= box[2]
                               and box[1] <= bbox[3] and bbox[1] <= box[3]):
                continue
            target = region_bg / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            kept.append(entry.strip())
        if kept:
            write_lst_file(region_bg / index_path.name, kept)
            total += len(kept)
    return total


EAIP_AIRSPACE = "NATS UK eAIP ENR 2.1 / AD 2.17"
OPENAIP_AIRSPACE = "openAIP daily exports (CC BY-NC 4.0)"


def airspace_label(bg_dir: Path) -> str:
    """The manifest's airspace source, read off the drawings in ``CONFIG/bg``.

    Every ``OAIP*.lst`` group is openAIP's and every other ``.lst`` group is
    drawn from the eAIP, so the label credits exactly the sources whose
    drawings the package holds, whatever an earlier manifest said.
    """
    bg_dir = Path(bg_dir)
    indexes = [path.name for path in bg_dir.glob("*.lst")] if bg_dir.is_dir() else []
    named = []
    if any(not OPENAIP_INDEX_RE.match(name) for name in indexes):
        named.append(EAIP_AIRSPACE)
    if any(OPENAIP_INDEX_RE.match(name) for name in indexes):
        named.append(OPENAIP_AIRSPACE)
    return ", and ".join(named) if named else "none in this package"


def _refuse_local_paths(manifest) -> None:
    leaked = pack.local_paths_in(manifest)
    if leaked:
        raise RuntimeError(
            "The manifest would publish a path from this machine (%s). "
            "Describe the source in words instead." % ", ".join(leaked))


# --- The world ZIP (skymaps) -------------------------------------------------

def build_global_zip(stage_dir: Path, out_dir: Path, airac_display: str,
                     airac_code: str, generator_version="",
                     notes_out=None, log=None, sources=None, counts=None) -> Path:
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
        sources=sources, counts=counts, generator_version=generator_version,
    )
    _refuse_local_paths(manifest)
    # The staged tree belongs to the combined build. Its manifest is put back
    # once the world ZIP is written, so a tree kept with --keep-staging still
    # says region EG and a later --only run accepts it.
    staged_manifest = Path(stage_dir) / pack.MANIFEST_NAME
    combined = staged_manifest.read_bytes() if staged_manifest.is_file() else None
    try:
        pack.write_manifest(stage_dir, manifest)
        zip_path = pack.make_zip(stage_dir, Path(out_dir) / pack.asset_name("ALL", airac_code))
    finally:
        if combined is not None:
            staged_manifest.write_bytes(combined)
        elif staged_manifest.exists():
            staged_manifest.unlink()
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
                     generator_version="", log=None, sources=None,
                     staged_airspace: bool = False) -> Path | None:
    """Package one ICAO region's subset of the staged tree as <prefix>_<airac>.zip.

    ``base_airspaces`` is the openAIP airspace list when the build draws
    airspace from openAIP, and None when it does not (eAIP airspace only, or
    airspace skipped): the region then ships without drawings. They are
    written as the per-country groups SkyRadar reads, with the combined
    build's ``OAIP-countries`` map. With ``staged_airspace`` the openAIP
    drawings already in the staged tree are copied instead, untouched, and
    ``base_airspaces`` is not used. ``sources`` is the combined build's source
    list, carried into the region's manifest.
    """
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
        endpoints = set(filtered_fixes) | _parse_navaid_idents(nav_target / "navaid.dat")
        counts["airway_segments"] = _filter_airways(nav_source / "awy.dat", nav_target / "awy.dat", endpoints)
    if (nav_source / "vfr-points").is_file():
        counts["vfr_points"] = _filter_vfr_points(nav_source / "vfr-points", nav_target / "vfr-points", bbox)
    if (nav_source / "NOTICE.txt").is_file():
        nav_target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(nav_source / "NOTICE.txt", nav_target / "NOTICE.txt")
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

    region_bg = region_stage / "CONFIG" / "bg"
    added = 0
    if staged_airspace:
        added = copy_staged_airspace_groups(Path(stage_dir) / "CONFIG" / "bg", region_bg, bbox)
    elif base_airspaces:
        added = write_country_airspace_groups(region_bg, base_airspaces, bbox, airac_display)
    if staged_airspace or base_airspaces:
        counts["openaip_airspaces"] = added
        country_map = Path(stage_dir) / "CONFIG" / "bg" / "OAIP-countries"
        if added and country_map.is_file():
            shutil.copyfile(country_map, region_bg / "OAIP-countries")

    region_sources = dict(sources or {})
    region_sources["subset"] = "filtered from the combined build by ICAO region prefix"
    if counts.get("openaip_airspaces"):
        region_sources["airspace"] = airspace_label(region_bg)
    else:
        region_sources["airspace"] = "none in this region package"
    manifest = pack.build_manifest(
        region_stage, region=prefix, airac=airac_display, airac_code=airac_code,
        sources=region_sources,
        counts=counts, generator_version=generator_version,
    )
    _refuse_local_paths(manifest)
    pack.write_manifest(region_stage, manifest)
    zip_path = pack.make_zip(region_stage, Path(out_dir) / pack.asset_name(prefix, airac_code))
    problems = pack.verify_zip(zip_path, manifest)
    if problems:
        raise RuntimeError("Region %s ZIP failed verification: %s" % (prefix, "; ".join(problems)))
    _log("region %s: %d file(s) -> %s" % (prefix, len(manifest["files"]), zip_path.name))
    shutil.rmtree(region_stage, ignore_errors=True)
    return zip_path
