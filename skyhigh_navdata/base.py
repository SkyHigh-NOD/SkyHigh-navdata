"""Global base data: OurAirports + openAIP daily exports.

The SkyHigh-generated navdata (NATS eAIP) is authoritative for the EG region,
but a SkyRadar that travels needs worldwide coverage underneath it. Two free
sources provide that base, merged *under* the SkyHigh data:

    precedence:  SkyHigh eAIP/navdata  >  openAIP  >  OurAirports

- **openAIP** (https://www.openaip.net/) publishes anonymous daily exports of
  its whole database at ``https://storage.openaip.net/openaip-system-exports/``
  as per-country ND-GeoJSON files (``gb_apt.ndgeojson`` ...). No API key is
  needed. We use the airports (``apt``), navaids (``nav``), reporting points
  (``rpp``) and airspaces (``asp``) exports.
- **OurAirports** (https://davidmegginson.github.io/ourairports-data/) is the
  worldwide aerodrome database, used as the position fallback for any ICAO
  openAIP does not list.

Both are cached under ``.cache/``: an S3 ETag (openAIP) or a source-control
SHA (OurAirports) decides whether a file needs re-downloading, so a rebuild
only touches what actually changed.
"""
from __future__ import annotations

import csv
import io
import json
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

OPENAIP_EXPORTS_URL = "https://storage.openaip.net/openaip-system-exports/"
OPENAIP_TYPES = ("apt", "nav", "rpp", "asp")

OURAIRPORTS_REPO = "davidmegginson/ourairports-data"
OURAIRPORTS_CSV = "airports.csv"

USER_AGENT = "skyhigh-navdata base-data fetcher"

NDGEOJSON_KEY_RE = re.compile(r"^[a-z]{2}_(?:%s)\.ndgeojson$" % "|".join(OPENAIP_TYPES))


def _fetch(url: str, timeout: int = 120) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _download_to(url: str, path: Path, timeout: int = 300) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(_fetch(url, timeout=timeout))
    temporary.replace(path)


# --- openAIP -----------------------------------------------------------------

def load_openaip(cache_dir: Path, log=None) -> dict:
    """Download and parse the openAIP exports into one dataset.

    Returns a dict with ``airports`` (icao → record), ``navaids``
    (ident → record), ``fixes`` (name → (lat, lon)) and ``airspaces`` (list),
    plus a ``counts`` dict.
    """
    cache_dir = Path(cache_dir)
    # full listing with LastModified stamps, so unchanged files stay cached
    listing = _openaip_listing(cache_dir, log)
    cache_path = cache_dir / "openaip.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.is_file() else {}
    except ValueError:
        cache = {}

    airports: dict = {}
    navaids: dict = {}
    fixes: dict = {}
    airspaces: list = []

    changed = 0
    for key, info in sorted(listing.items()):
        stamp = info.get("last_modified", "")
        path = cache_dir / "openaip" / key.replace("_", "__")
        if cache.get(key) != stamp or not path.is_file():
            cache[key] = stamp
            try:
                _download_to(info["url"], path)
                changed += 1
            except (urllib.error.URLError, OSError) as error:
                if log:
                    log("openAIP: WARNING - %s download failed (%s); skipped" % (key, error))
                continue
        # Cached files are always parsed: a parse failure in an earlier run
        # must not leave a file downloaded-but-unusable.
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            _absorb_openaip_record(key, record, airports, navaids, fixes, airspaces)
    if changed:
        cache_path.write_text(json.dumps(cache, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    if log:
        log("openAIP: %d file(s) downloaded, %d airports, %d navaids, %d reporting points, "
            "%d airspaces" % (changed, len(airports), len(navaids), len(fixes), len(airspaces)))

    return {
        "airports": airports,
        "navaids": navaids,
        "fixes": fixes,
        "airspaces": airspaces,
        "counts": {
            "base_openaip_airports": len(airports),
            "base_openaip_navaids": len(navaids),
            "base_openaip_fixes": len(fixes),
            "base_openaip_airspaces": len(airspaces),
        },
    }


def _openaip_listing(cache_dir: Path, log=None) -> dict:
    """Every interesting export key → {url, last_modified}."""
    out = {}
    token = None
    while True:
        url = OPENAIP_EXPORTS_URL + "?list-type=2"
        if token:
            url += "&continuation-token=" + urllib.parse.quote(token)
        xml = _fetch(url).decode("utf-8", errors="replace")
        entries = re.findall(
            r"<Contents>(.*?)</Contents>", xml, re.DOTALL)
        for entry in entries:
            key_match = re.search(r"<Key>([^<]+)</Key>", entry)
            stamp_match = re.search(r"<LastModified>([^<]+)</LastModified>", entry)
            if not key_match:
                continue
            key = key_match.group(1)
            if not NDGEOJSON_KEY_RE.match(key):
                continue
            out[key] = {
                "url": OPENAIP_EXPORTS_URL + key,
                "last_modified": stamp_match.group(1) if stamp_match else "",
            }
        token_match = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", xml)
        if not token_match:
            break
        token = token_match.group(1)
    return out


def _absorb_openaip_record(key, record, airports, navaids, fixes, airspaces) -> None:
    """Fold one ND-GeoJSON feature into the dataset by its export type."""
    kind = key.split("_", 1)[1].split(".", 1)[0]
    properties = record.get("properties") or {}
    geometry = record.get("geometry") or {}
    coordinates = geometry.get("coordinates")
    if kind == "apt":
        icao = str(properties.get("icaoCode") or "").upper()
        if not re.match(r"^[A-Z0-9]{4}$", icao) or icao in airports:
            return
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            return
        airports[icao] = {
            "lat": coordinates[1],
            "lon": coordinates[0],
            "name": str(properties.get("name") or icao).strip() or icao,
            "country": str(properties.get("country") or ""),
        }
    elif kind == "nav":
        ident = str(properties.get("identifier") or "").upper()
        if not ident or ident in navaids:
            return
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            return
        navaids[ident] = {
            "lat": coordinates[1],
            "lon": coordinates[0],
            "name": str(properties.get("name") or ident).strip() or ident,
            "type": _openaip_nav_row_type(properties.get("type")),
            "freq": re.sub(r"[^0-9.]", "", str((properties.get("frequency") or {}).get("value") or "")) or "0",
            "elev_ft": _altitude_ft(properties.get("elevation")),
            "region": str(properties.get("country") or "").upper() or "XX",
        }
    elif kind == "rpp":
        name = str(properties.get("name") or "").upper().strip()
        if not name or name in fixes:
            return
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            return
        fixes[name] = (coordinates[1], coordinates[0])
    elif kind == "asp":
        rings = _polygon_rings(coordinates)
        if not rings:
            return
        airspaces.append({
            "name": str(properties.get("name") or "UNNAMED").strip() or "UNNAMED",
            "class": str(properties.get("icaoClass") or ""),
            "country": str(properties.get("country") or "").upper(),
            "upper": _altitude_text(properties.get("upperLimit")),
            "lower": _altitude_text(properties.get("lowerLimit")),
            "rings": rings,
        })


_NAV_TYPE_MAP = {1: 2, 2: 2, 4: 2, 7: 2, 3: 3, 8: 3, 5: 5, 6: 5}


def _openaip_nav_row_type(kind) -> int:
    """openAIP navaid type enum → the row type SkyRadar's navaid.dat uses
    (2 = VOR family, 3 = DME, 5 = NDB family — mirroring NAV_TYPES in build.py)."""
    try:
        return _NAV_TYPE_MAP.get(int(kind), 2)
    except (TypeError, ValueError):
        return 2


def _altitude_ft(limit) -> int | None:
    """openAIP altitude dict → feet (FL value divided per SkyRadar convention:
    the raw number is kept, the unit decides the display in .draw headers)."""
    if not isinstance(limit, dict):
        return None
    value = limit.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    unit = limit.get("unit")
    if unit == 1:  # feet
        return int(round(value))
    if unit == 0:  # metres
        return int(round(value * 3.28084))
    return int(round(value))  # FL / other: keep the number


def _altitude_text(limit) -> str:
    """openAIP altitude dict → eAIP-style display text ('FL105', '3500 ft')."""
    if not isinstance(limit, dict):
        return ""
    value = limit.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return ""
    unit = limit.get("unit")
    if unit == 6:  # flight level
        return "FL%d" % int(round(value))
    feet = _altitude_ft(limit)
    if feet is None:
        return ""
    return "%d ft" % feet


def _polygon_rings(coordinates):
    """GeoJSON Polygon/MultiPolygon coordinates → [outer ring [(lat, lon)]] ."""
    polygons = []
    if not isinstance(coordinates, list):
        return polygons
    if geometry_is_multipolygon(coordinates):
        for polygon in coordinates:
            if polygon and isinstance(polygon[0], list):
                polygons.append(polygon[0])
    else:
        polygons.append(coordinates[0])
    rings = []
    for ring in polygons:
        if not ring or not isinstance(ring, list):
            continue
        points = []
        for coord in ring:
            if isinstance(coord, (list, tuple)) and len(coord) >= 2:
                points.append((coord[1], coord[0]))  # (lat, lon)
        if len(points) >= 3:
            rings.append(points)
    return rings


def geometry_is_multipolygon(coordinates) -> bool:
    return bool(coordinates) and isinstance(coordinates[0], list) and \
        bool(coordinates[0]) and isinstance(coordinates[0][0], list) and \
        bool(coordinates[0][0]) and isinstance(coordinates[0][0][0], list)


# --- OurAirports -------------------------------------------------------------

def load_ourairports(cache_dir: Path, log=None) -> dict:
    """Download (SHA-cached) and parse the OurAirports CSV.

    Returns ``icao → {lat, lon, name, country}`` for every non-closed entry
    with a usable 4-character ident.
    """
    cache_dir = Path(cache_dir)
    cache_path = cache_dir / "ourairports.json"
    cached_sha = ""
    if cache_path.is_file():
        try:
            cached_sha = json.loads(cache_path.read_text(encoding="utf-8")).get("sha", "")
        except ValueError:
            cached_sha = ""

    sha = ""
    try:
        commits = json.loads(
            _fetch("https://api.github.com/repos/%s/commits?path=%s&per_page=1"
                   % (OURAIRPORTS_REPO, OURAIRPORTS_CSV)).decode("utf-8"))
        if commits and isinstance(commits, list):
            sha = str(commits[0].get("sha") or "")
    except (urllib.error.URLError, ValueError, OSError) as error:
        if log:
            log("OurAirports: WARNING - could not check the latest revision (%s)" % error)
    if sha and sha == cached_sha and (cache_dir / "ourairports.csv").is_file():
        if log:
            log("OurAirports: %s is current - reusing the cached CSV" % sha[:12])
    elif sha:
        if log:
            log("OurAirports: downloading %s (%s...)" % (OURAIRPORTS_CSV, sha[:12]))
        try:
            _download_to(
                "https://raw.githubusercontent.com/%s/%s/%s"
                % (OURAIRPORTS_REPO, sha, OURAIRPORTS_CSV),
                cache_dir / "ourairports.csv",
            )
            cache_path.write_text(json.dumps({"sha": sha}) + "\n", encoding="utf-8")
        except (urllib.error.URLError, OSError) as error:
            if log:
                log("OurAirports: WARNING - download failed (%s)" % error)
            sha = ""

    csv_path = cache_dir / "ourairports.csv"
    if not csv_path.is_file():
        return {"airports": {}, "counts": {"base_ourairports_airports": 0}}

    airports: dict = {}
    text = csv_path.read_text(encoding="utf-8", errors="replace")
    for row in csv.DictReader(io.StringIO(text)):
        ident = str(row.get("ident") or "").upper()
        if not re.match(r"^[A-Z0-9]{4}$", ident) or ident in airports:
            continue
        if str(row.get("type") or "") == "closed":
            continue
        try:
            lat = float(row["latitude_deg"])
            lon = float(row["longitude_deg"])
        except (KeyError, ValueError):
            continue
        airports[ident] = {
            "lat": lat,
            "lon": lon,
            "name": str(row.get("name") or ident).strip() or ident,
            "country": str(row.get("iso_country") or ""),
        }
    if log:
        log("OurAirports: %d aerodromes parsed" % len(airports))
    return {
        "airports": airports,
        "counts": {"base_ourairports_airports": len(airports)},
    }
