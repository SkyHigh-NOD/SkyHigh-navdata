"""Merge the global base data *underneath* the SkyHigh-generated nav files.

The staged ``CONFIG/nav`` files come out of the eAIP build (and hand-maintained
overrides) first, so they are already on top. This module only ever **appends**
base entries whose identity is not already present:

    fix.dat        eAIP fixes + openAIP reporting points
    navaid.dat     eAIP navaids + openAIP navaids
    AD-positions   eAIP aerodromes + openAIP aerodromes + OurAirports

Precedence therefore falls out of the insertion order: SkyHigh > openAIP >
OurAirports — "whichever is most up to date goes on top" without any compare
logic at read time.
"""
from __future__ import annotations

import re
from pathlib import Path

AD_POSITION_LINE_RE = re.compile(r"^([A-Z0-9]{4}) (-?\d+\.\d+),(-?\d+\.\d+) (.+)$")


def _read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _atomic_write_text(path: Path, text: str) -> None:
    import os
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _fallback_fix_rows() -> list[str]:
    """Worldwide fixes from the bundled X-Plane earth-fix.dat (Robin A. Peel's
    navdata, GPL v2+ — the header inside states the licence). This is the
    lowest-precedence layer under the eAIP fixes and openAIP reporting points,
    which alone cover mostly Europe."""
    path = Path(__file__).resolve().parent / "resources" / "earth-fix.dat"
    rows = []
    try:
        for line in path.read_text(encoding="ascii", errors="replace").splitlines():
            parts = line.split()
            if len(parts) == 3 and re.match(r"^-?\d+(?:\.\d+)?$", parts[0]) \
                    and re.match(r"^-?\d+(?:\.\d+)?$", parts[1]):
                rows.append(line.strip())
    except OSError:
        pass
    return rows


def merge_fixes(nav_dir: Path, base: dict, log=None) -> tuple[int, int]:
    """Append base fixes to fix.dat (plain lat lon name rows).

    Precedence is insertion order: the eAIP fixes are already in the file, so
    openAIP reporting points are appended first and the bundled worldwide
    X-Plane fix set last — names already present are never duplicated.
    Returns (openaip_added, fallback_added)."""
    existing = set()
    lines = _read_lines(Path(nav_dir) / "fix.dat")
    for line in lines:
        parts = line.split()
        if len(parts) >= 3:
            existing.add(parts[2].upper())
    added = []
    for name, (lat, lon) in sorted(base.get("fixes", {}).items()):
        if name in existing:
            continue
        existing.add(name)
        added.append("%.6f %.6f %s" % (lat, lon, name))
    openaip_added = len(added)
    fallback_added = 0
    for row in _fallback_fix_rows():
        parts = row.split()
        if parts[2].upper() in existing:
            continue
        existing.add(parts[2].upper())
        added.append(row)
        fallback_added += 1
    if added:
        _atomic_write_text(
            Path(nav_dir) / "fix.dat",
            "\n".join(lines + added) + "\n",
        )
    if log:
        log("fixes: +%d openAIP reporting point(s), +%d worldwide fallback"
            % (openaip_added, fallback_added))
    return openaip_added, fallback_added


def merge_navaids(nav_dir: Path, base: dict, log=None) -> int:
    """Append openAIP navaids to navaid.dat (rows before the trailing 99)."""
    path = Path(nav_dir) / "navaid.dat"
    lines = _read_lines(path)
    if not lines:
        return 0
    existing = set()
    for line in lines:
        parts = line.split()
        if len(parts) >= 11:
            existing.add(parts[7].upper())
    added = []
    for ident, nav in sorted(base.get("navaids", {}).items()):
        if ident in existing:
            continue
        existing.add(ident)
        added.append(
            "%s %.6f %.6f %s %s 0 0 %s 0 %s %s" % (
                nav.get("type", 2), nav["lat"], nav["lon"], nav.get("elev_ft", 0),
                nav.get("freq", "0"), ident, nav.get("region", "XX"), nav.get("name", ident),
            )
        )
    if added:
        # rows go before the trailing "99" terminator
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

    The file ends with a record-count line that is rewritten.
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
    for icao, entry in sorted(base.get("airports", {}).items()):
        if icao in existing:
            continue
        existing.add(icao)
        records.append("%s %.6f,%.6f %s" % (icao, entry["lat"], entry["lon"], entry["name"]))
        added += 1
    for icao, entry in sorted((base.get("ourairports") or {}).get("airports", {}).items()):
        if icao in existing:
            continue
        existing.add(icao)
        records.append("%s %.6f,%.6f %s" % (icao, entry["lat"], entry["lon"], entry["name"]))
        added += 1
    if records:
        _atomic_write_text(path, "\n".join(records + [str(len(records))]) + "\n")
    if log:
        log("aerodromes: +%d base position(s)" % added)
    return added


def merge_base_into_stage(stage_dir: Path, base: dict, log=None) -> dict:
    """Apply all base merges to a staged CONFIG tree. Returns counts."""
    nav_dir = Path(stage_dir) / "CONFIG" / "nav"
    openaip_fixes, fallback_fixes = merge_fixes(nav_dir, base, log)
    counts = {
        "base_fixes": openaip_fixes,
        "base_fallback_fixes": fallback_fixes,
        "base_navaids": merge_navaids(nav_dir, base, log),
        "base_aerodromes": merge_aerodromes(nav_dir, base, log),
    }
    if log:
        log("base merge: %s" % ", ".join("%s=%d" % item for item in counts.items()))
    return counts
