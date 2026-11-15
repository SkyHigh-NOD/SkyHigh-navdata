"""Merge the global base data *underneath* the SkyHigh-generated nav files.

The staged ``CONFIG/nav`` files come out of the eAIP build (and the
hand-maintained overrides) first, so they are already on top. This module only
ever **appends** base entries whose identity is not already present:

    navaid.dat     eAIP navaids + openAIP navaids
    AD-positions   eAIP aerodromes + openAIP aerodromes + OurAirports

Precedence falls out of the insertion order: SkyHigh > openAIP > OurAirports.

``fix.dat`` is left alone. It is the file IFR routes resolve against and its
rows are ``lat lon NAME`` split on whitespace; openAIP's reporting points are
VFR points whose names contain spaces and repeat between aerodromes. They go
to their own file, ``CONFIG/nav/vfr-points``.
"""
from __future__ import annotations

import math
import os
import re
from pathlib import Path

AD_POSITION_LINE_RE = re.compile(r"^([A-Z0-9]{4}) (-?\d+\.\d+),(-?\d+\.\d+) (.+)$")
ICAO_RE = re.compile(r"^[A-Z]{4}$")
FIX_NAME_RE = re.compile(r"^[A-Z0-9]+$")

# Two navaids with one ident are the same navaid when they sit this close.
SAME_NAVAID_NM = 10.0

VFR_POINTS_NAME = "vfr-points"
NOTICE_NAME = "NOTICE.txt"

NOTICE_TEXT = """\
This package contains data from third parties.

openAIP (https://www.openaip.net)
  Airports, navaids, VFR reporting points and airspace from the openAIP
  daily exports. Licensed CC BY-NC 4.0
  (https://creativecommons.org/licenses/by-nc/4.0/). Non-commercial use only;
  keep this credit with the data.

OurAirports (https://ourairports.com)
  Aerodrome positions. Public domain.

NATS UK eAIP (https://nats-uk.ead-it.com)
  UK navaids, fixes, airways, aerodromes and airspace.

X-Plane Scenery Gateway (https://gateway.x-plane.com)
  Airport layouts in CONFIG/ad.
"""


def _read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _one_line(text) -> str:
    """Text with every run of whitespace, tabs and newlines included, as one space."""
    return " ".join(str(text or "").split())


def distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3440.065 * 2 * math.asin(min(1.0, math.sqrt(a)))


def navaid_row(nav: dict) -> str:
    """One navaid.dat row for a base navaid. The elevation is always a number."""
    ident = str(nav["ident"]).upper()
    elevation = nav.get("elev_ft")
    if not isinstance(elevation, (int, float)) or isinstance(elevation, bool):
        elevation = 0
    return "%s %.6f %.6f %d %s 0 0 %s 0 %s %s" % (
        nav.get("type", 2), nav["lat"], nav["lon"], int(round(elevation)),
        nav.get("freq") or "0", ident, nav.get("region") or "XX",
        _one_line(nav.get("name")) or ident,
    )


def write_vfr_points(nav_dir: Path, base: dict, log=None) -> int:
    """Write openAIP's VFR reporting points to ``CONFIG/nav/vfr-points``.

    One point per line, tab-separated: latitude, longitude, ISO country, name.
    The name is the last field, so it may hold spaces. Nothing is written when
    there are no points; a file from an earlier run is removed in that case.
    """
    path = Path(nav_dir) / VFR_POINTS_NAME
    points = base.get("vfr_points") or {}
    rows = []
    for key, (lat, lon) in sorted(points.items()):
        country, name = key[0], key[1]
        rows.append("%.6f\t%.6f\t%s\t%s" % (lat, lon, country or "XX", _one_line(name)))
    if not rows:
        if path.exists():
            path.unlink()
        return 0
    header = [
        "# VFR reporting points from the openAIP daily exports (CC BY-NC 4.0).",
        "# latitude<TAB>longitude<TAB>ISO country<TAB>name",
    ]
    _atomic_write_text(path, "\n".join(header + rows) + "\n")
    if log:
        log("vfr points: %d openAIP reporting point(s)" % len(rows))
    return len(rows)


def merge_navaids(nav_dir: Path, base: dict, log=None) -> int:
    """Append openAIP navaids to navaid.dat (rows before the trailing 99).

    Idents repeat around the world, so a base navaid is only a duplicate of a
    row already in the file when the ident matches *and* the two are within
    ``SAME_NAVAID_NM`` of each other. The eAIP's ``LON`` keeps its place and a
    navaid with the same ident on another continent is still added.
    """
    path = Path(nav_dir) / "navaid.dat"
    lines = _read_lines(path)
    if not lines:
        return 0
    placed: dict = {}
    for line in lines:
        parts = line.split()
        if len(parts) >= 11:
            try:
                placed.setdefault(parts[7].upper(), []).append((float(parts[1]), float(parts[2])))
            except ValueError:
                continue
    added = []
    for _key, nav in sorted((base.get("navaids") or {}).items()):
        ident = str(nav.get("ident") or "").upper()
        if not ident or not FIX_NAME_RE.match(ident):
            continue
        try:
            lat, lon = float(nav["lat"]), float(nav["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if any(distance_nm(lat, lon, other_lat, other_lon) <= SAME_NAVAID_NM
               for other_lat, other_lon in placed.get(ident, ())):
            continue
        placed.setdefault(ident, []).append((lat, lon))
        added.append(navaid_row(dict(nav, ident=ident, lat=lat, lon=lon)))
    if added:
        write_lines = list(lines)
        if write_lines and write_lines[-1].strip() == "99":
            write_lines = write_lines[:-1] + added + ["99"]
        else:
            write_lines = write_lines + added
        _atomic_write_text(path, "\n".join(write_lines) + "\n")
    if log:
        log("navaids: +%d openAIP navaid(s)" % len(added))
    return len(added)


def merge_aerodromes(nav_dir: Path, base: dict, log=None) -> int:
    """Append openAIP + OurAirports aerodromes to AD-positions.

    Only four-letter ICAO codes are added. The file ends with a record-count
    line that is rewritten.
    """
    path = Path(nav_dir) / "AD-positions"
    lines = _read_lines(path)
    records = []
    existing = set()
    for line in lines:
        match = AD_POSITION_LINE_RE.match(line)
        if not match:
            continue
        existing.add(match.group(1))
        records.append(line.rstrip())
    added = 0
    layers = (base.get("airports") or {}, (base.get("ourairports") or {}).get("airports") or {})
    for layer in layers:
        for icao, entry in sorted(layer.items()):
            if not ICAO_RE.match(str(icao)) or icao in existing:
                continue
            existing.add(icao)
            records.append("%s %.6f,%.6f %s" % (
                icao, entry["lat"], entry["lon"], _one_line(entry.get("name")) or icao))
            added += 1
    if records:
        _atomic_write_text(path, "\n".join(records + [str(len(records))]) + "\n")
    if log:
        log("aerodromes: +%d base position(s)" % added)
    return added


def _tail_count(counts, key, limit):
    """The recorded number of base rows, or None when it cannot be trusted."""
    value = (counts or {}).get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= limit else None


def strip_base_rows(stage_dir: Path, manifest=None, region: str = "EG", log=None) -> dict:
    """Take the base rows of an earlier merge back out of a seeded tree.

    A tree unpacked from a published ZIP already holds the rows an earlier
    build merged in. Merging on top of them would keep every one, so an
    aerodrome OurAirports supplied then would still win after openAIP gained
    it, and rows a source has since dropped would stay. This removes them, so
    the merge that follows starts from the SkyHigh rows alone.

    The merge appends, so the base rows are the last ones of each file, and a
    manifest written by this builder records how many there are
    (``base_rows_aerodromes``, ``base_rows_navaids``). A seed without that
    record (an older build) is handled by what its rows say: in AD-positions
    only the ``region`` idents (the ones the eAIP build produces) are kept,
    unless the file is listed as an override, in which case it is left as it
    is. navaid.dat is left alone without a record: its rows do not say where
    they came from, and openAIP is its only base source.

    Returns how many rows were removed from each file.
    """
    manifest = manifest or {}
    counts = manifest.get("counts") or {}
    overridden = set(manifest.get("overrides") or [])
    nav_dir = Path(stage_dir) / "CONFIG" / "nav"
    removed = {"aerodromes": 0, "navaids": 0}

    path = nav_dir / "AD-positions"
    records = [line.rstrip() for line in _read_lines(path) if AD_POSITION_LINE_RE.match(line)]
    if records:
        tail = _tail_count(counts, "base_rows_aerodromes", len(records))
        if tail is not None:
            kept = records[:len(records) - tail]
        elif "CONFIG/nav/AD-positions" in overridden:
            kept = records
        else:
            kept = [line for line in records if line.startswith(region)]
        if len(kept) != len(records):
            removed["aerodromes"] = len(records) - len(kept)
            _atomic_write_text(path, "\n".join(kept + [str(len(kept))]) + "\n")

    path = nav_dir / "navaid.dat"
    lines = _read_lines(path)
    closed = bool(lines) and lines[-1].strip() == "99"
    body = lines[:-1] if closed else lines
    tail = _tail_count(counts, "base_rows_navaids", len(body))
    if tail:
        removed["navaids"] = tail
        _atomic_write_text(path, "\n".join(body[:len(body) - tail] + (["99"] if closed else [])) + "\n")

    if log:
        log("seed: %d base aerodrome row(s) and %d base navaid row(s) taken out before the merge"
            % (removed["aerodromes"], removed["navaids"]))
    return removed


def write_notice(stage_dir: Path) -> Path:
    """Write the third-party credits into the package (``CONFIG/nav/NOTICE.txt``)."""
    path = Path(stage_dir) / "CONFIG" / "nav" / NOTICE_NAME
    _atomic_write_text(path, NOTICE_TEXT)
    return path


def merge_base_into_stage(stage_dir: Path, base: dict, log=None) -> dict:
    """Apply all base merges to a staged CONFIG tree. Returns counts."""
    nav_dir = Path(stage_dir) / "CONFIG" / "nav"
    counts = {
        "base_vfr_points": write_vfr_points(nav_dir, base, log),
        "base_navaids": merge_navaids(nav_dir, base, log),
        "base_aerodromes": merge_aerodromes(nav_dir, base, log),
    }
    # Both merges append, so these are also the rows at the end of each file.
    # strip_base_rows() reads them when a later run starts from this package.
    counts["base_rows_navaids"] = counts["base_navaids"]
    counts["base_rows_aerodromes"] = counts["base_aerodromes"]
    write_notice(stage_dir)
    if log:
        log("base merge: %s" % ", ".join("%s=%d" % item for item in counts.items()))
    return counts
