#!/usr/bin/env python3
"""Build a complete SkyRadar AIRAC release using the updater's exact NATS logic.

The release is written as SkyRadar-AIRAC2609/ (or the selected cycle) and is
never pushed to GitHub automatically.

Usage:
    python3 build_airac_release_fixed.py --airac 2609

Dependencies:
    pip install beautifulsoup4 lxml
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from zipfile import ZipFile

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
import warnings
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

NATS_AIP_URL = "https://nats-uk.ead-it.com/cms-nats/opencms/en/Publications/AIP/"
GATEWAY_API_BASE = "https://gateway.x-plane.com/apiv1"
GATEWAY_WEB_BASE = "https://gateway.x-plane.com"
REGION = "EG"
HIGH_LOW_BOUNDARY = 245

LAT_RE = re.compile(r"^(\d{2})(\d{2})(\d{2}(?:\.\d+)?)([NS])$")
LON_RE = re.compile(r"^(\d{3})(\d{2})(\d{2}(?:\.\d+)?)([EW])$")
TAG_RE = re.compile(r"<[^>]+>")
POINT_TAGS = {"TDESIGNATED_POINT": "11", "TNDB": "2", "TVOR": "3", "TDME": "3"}
NAV_TYPES = {"VOR": 2, "VORDME": 2, "DME": 3, "NDB": 5, "ILSLOC": 4, "ILSGP": 12, "ILSDME": 3}
SD_PAIR_RE = re.compile(r'<span[^>]*class="SD"[^>]*>(?P<val>.*?)</span>\s*<span[^>]*class="sdParams"[^>]*>(?P<meta>.*?)</span>', re.DOTALL | re.IGNORECASE)
BAD_NAVPOINTS = {"AD","AIP","AIR","AIRCRAFT","AIRWAY","AIRWAYS","AIS","AND","APP","APPROACH","ARRIVAL","ARRIVALS","AT","ATC","ATIS","ATS","CAT","CTR","DCT","DEPART","DEPARTURE","DEPARTURES","DME","EAST","EMAIL","ENR","ENTRY","EXIT","FIR","FL","FOR","FROM","FT","GA","GROUND","IFR","INBOUND","MET","NATS","NDB","NM","NORTH","NOTAM","ONE","POINT","PPR","ROUTE","ROUTES","RUNWAY","RWY","SID","SIDS","SOUTH","STAR","STARS","TMA","THE","THEN","THREE","TO","TRACK","TWO","UK","UNTIL","VFR","VIA","VOR","WEST","WITH"}


def log(message: str):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {message}", flush=True)


def request_url(url: str) -> Request:
    return Request(url, headers={"User-Agent": "SkyRadar AIRAC Updater/1.0"})


def fetch_text(url: str, timeout: int = 60) -> str:
    with urlopen(request_url(url), timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def download_file(url: str, destination: Path, timeout: int = 120):
    with urlopen(request_url(url), timeout=timeout) as response, destination.open("wb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)


def find_current_nats_airac() -> tuple[str, str]:
    """Exact logic from the current updater: page -> AIRAC cycle + ZIP href."""
    page = fetch_text(NATS_AIP_URL, timeout=30)
    cycle = re.search(r"AIRAC\s+(\d{2})\s*/\s*(\d{4})", page, re.I)
    archive = re.search(r'href=["\']([^"\']*AIRAC-\d{2}-\d{2}\.zip[^"\']*)["\']', page, re.I)
    if not cycle or not archive:
        raise RuntimeError("Could not find the current NATS AIRAC ZIP download link.")
    return "%s/%s" % (cycle.group(1), cycle.group(2)[-2:]), urljoin(NATS_AIP_URL, archive.group(1))


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", value)).strip()


def extract_entities(row):
    entities = defaultdict(lambda: defaultdict(list))
    for match in SD_PAIR_RE.finditer(str(row)):
        parts = clean(match.group("meta")).split(";")
        if len(parts) == 3:
            tag, field, entity_id = parts
            entities[(tag, entity_id)][field].append(clean(match.group("val")))
    return entities


def first(entity, field, default=None):
    values = entity.get(field)
    return values[0] if values else default


def dms_to_decimal(text: str, is_lat: bool) -> float:
    text = re.sub(r"[^0-9NSEW.]", "", text.strip().upper())
    match = (LAT_RE if is_lat else LON_RE).fullmatch(text)
    if match is None:
        raise ValueError(f"Invalid coordinate: {text}")
    degrees, minutes, seconds, hemisphere = match.groups()
    value = float(degrees) + float(minutes) / 60 + float(seconds) / 3600
    return -value if hemisphere in "SW" else value


def point_identity(row):
    for (tag, _entity_id), fields in extract_entities(row).items():
        if tag in POINT_TAGS and first(fields, "CODE_ID"):
            return first(fields, "CODE_ID").upper(), POINT_TAGS[tag]
    return None, None


def segment_limits(row):
    for (tag, _entity_id), fields in extract_entities(row).items():
        if tag != "TRTE_SEG":
            continue
        upper = first(fields, "VAL_DIST_VER_UPPER")
        lower = first(fields, "VAL_DIST_VER_LOWER")
        if upper is None or lower is None or first(fields, "UOM_DIST_VER_UPPER") != "FL":
            return None
        try:
            upper, lower = float(upper), float(lower)
        except ValueError:
            return None
        lower_uom = first(fields, "UOM_DIST_VER_LOWER")
        if lower_uom == "FT":
            lower /= 100
        elif lower_uom != "FL":
            return None
        return int(round(lower)), int(round(upper))
    return None


def build_airways_from_html(html_dir: Path, output_nav: Path, airac_cycle: str):
    candidates = sorted(html_dir.rglob("EG-ENR-3.2-en-GB.html")) or sorted(html_dir.rglob("*ENR-3.2*.html")) or sorted(html_dir.rglob("*ENR-3-2*.html"))
    if not candidates:
        raise FileNotFoundError("No ENR 3.2 ATS routes HTML found in extracted AIRAC.")
    source = candidates[0]
    log(f"Using airways file: {source.name}")
    soup = BeautifulSoup(source.read_text(encoding="utf-8", errors="replace"), "lxml")
    lines, skipped = [], 0
    for table in soup.find_all("table", class_="ENR-table"):
        bodies = table.find_all("tbody", recursive=False)
        if not bodies:
            continue
        rows = bodies[-1].find_all("tr", recursive=False)
        if len(rows) < 2:
            continue
        name = None
        for (tag, _entity_id), fields in extract_entities(rows[0]).items():
            if tag == "TEN_ROUTE_RTE":
                name = first(fields, "TXT_DESIG")
                break
        if not name:
            continue
        points, segments = [], []
        for row in rows[1:]:
            classes = row.get("class") or []
            if "Table-row-type-2" in classes:
                points.append(point_identity(row))
            elif "Table-row-type-3" in classes:
                segments.append(segment_limits(row))
        for index, limits in enumerate(segments):
            if index + 1 >= len(points):
                skipped += 1
                continue
            start_id, start_type = points[index]
            end_id, end_type = points[index + 1]
            if not start_id or not end_id or limits is None:
                skipped += 1
                continue
            base, top = limits
            bands = [(1, base, top)] if top <= HIGH_LOW_BOUNDARY else [(2, base, top)] if base >= HIGH_LOW_BOUNDARY else [(1, base, HIGH_LOW_BOUNDARY - 1), (2, HIGH_LOW_BOUNDARY, top)]
            for high_low, lower, upper in bands:
                lines.append(f"{start_id} {REGION} {start_type} {end_id} {REGION} {end_type} N {high_low} {lower} {upper} {name}")
    output_nav.mkdir(parents=True, exist_ok=True)
    (output_nav / "awy.dat").write_text("I\n1100 Version - data cycle %s, build %s, generated from UK eAIP ENR 3.2\n%s99\n" % (airac_cycle, datetime.now().strftime("%Y%m%d"), "\n".join(lines)), encoding="utf-8")
    log(f"Built {len(lines)} airway segments, skipped {skipped}")


def parse_fixes_html(source: Path):
    soup = BeautifulSoup(source.read_text(encoding="utf-8", errors="replace"), "lxml")
    records, seen = [], set()
    for row in soup.find_all("tr", id=lambda value: bool(value and value.upper().startswith("SP-"))):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 2:
            continue
        ident_span = cells[0].find("span", class_="SD")
        coords = cells[1].find_all("span", class_="SD")
        if not ident_span or len(coords) < 2:
            continue
        ident = ident_span.get_text(strip=True).upper()
        if not ident or ident in seen or "VP" in ident:
            continue
        try:
            lat = dms_to_decimal(coords[0].get_text(strip=True), True)
            lon = dms_to_decimal(coords[1].get_text(strip=True), False)
        except ValueError:
            continue
        seen.add(ident)
        records.append((lat, lon, ident))
    return records


def build_fixes_from_html(html_dir: Path, output_nav: Path):
    candidates = sorted(html_dir.rglob("*ENR-4.4*.html"))
    if not candidates:
        raise FileNotFoundError("No ENR 4.4 significant-points HTML found in extracted AIRAC.")
    fixes = []
    for source in candidates:
        log(f"Trying fixes file: {source.name}")
        fixes = parse_fixes_html(source)
        if fixes:
            break
    if not fixes:
        raise RuntimeError("No fixes extracted from ENR 4.4.")
    output_nav.mkdir(parents=True, exist_ok=True)
    (output_nav / "fix.dat").write_text("".join("%.6f %.6f %s\n" % item for item in fixes), encoding="utf-8")
    log(f"Wrote {len(fixes)} fixes")


def parse_navaids_html(source: Path):
    soup = BeautifulSoup(source.read_text(encoding="utf-8", errors="replace"), "lxml")
    records, seen = [], set()
    for row in soup.find_all("tr", id=lambda value: bool(value and value.upper().startswith("NAV-"))):
        match = re.search(r"NAV-(VOR|DME|VORDME|NDB|ILSLOC|ILSGP|ILSDME)-([A-Z0-9]+)", row.get("id", ""), re.I)
        if not match:
            continue
        nav_kind, ident = match.groups()
        nav_kind, ident = nav_kind.upper(), ident.upper()
        if ident in seen:
            continue
        fields = {}
        for span in row.find_all("span", class_="SD"):
            metadata = span.find_next_sibling("span", class_="sdParams")
            if metadata is None:
                continue
            raw = metadata.get_text(" ", strip=True)
            for field in ("VAL_FREQ", "GEO_LAT", "GEO_LONG", "VAL_ELEV", "TXT_NAME"):
                if f";{field};" in raw:
                    fields[field] = span.get_text(strip=True)
        if "GEO_LAT" not in fields or "GEO_LONG" not in fields:
            continue
        try:
            lat = dms_to_decimal(fields["GEO_LAT"], True)
            lon = dms_to_decimal(fields["GEO_LONG"], False)
        except ValueError:
            continue
        try:
            elevation = int(float(re.sub(r"[^0-9.-]", "", fields.get("VAL_ELEV", "0")) or 0))
        except ValueError:
            elevation = 0
        frequency = re.sub(r"[^0-9.]", "", fields.get("VAL_FREQ", "0")) or "0"
        if nav_kind in ("VOR", "VORDME") and "." not in frequency:
            frequency += ".000"
        records.append({"type": NAV_TYPES.get(nav_kind, 2), "lat": lat, "lon": lon, "elev": elevation, "freq": frequency, "ident": ident, "name": fields.get("TXT_NAME", ident).strip() or ident})
        seen.add(ident)
    return records


def build_navaids_from_html(html_dir: Path, output_nav: Path, airac_cycle: str):
    candidates = sorted(html_dir.rglob("EG-ENR-4.1-en-GB.html")) or sorted(html_dir.rglob("*ENR-4.1*.html"))
    if not candidates:
        raise FileNotFoundError("No ENR 4.1 navaids HTML found in extracted AIRAC.")
    records = parse_navaids_html(candidates[0])
    if not records:
        raise RuntimeError("No navaids extracted from ENR 4.1.")
    output_nav.mkdir(parents=True, exist_ok=True)
    lines = ["I", "1100 Version - data cycle %s, build %s, generated from UK eAIP ENR 4.1" % (airac_cycle, datetime.now().strftime("%Y%m%d"))]
    lines += ["%s %.6f %.6f %s %s 0 0 %s 0 %s %s" % (x["type"], x["lat"], x["lon"], x["elev"], x["freq"], x["ident"], REGION, x["name"]) for x in records]
    lines.append("99")
    (output_nav / "navaid.dat").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"Wrote {len(records)} navaids")


def tagged_value(container, field: str):
    for metadata in container.find_all("span", class_="sdParams"):
        if f";{field};" in metadata.get_text(" ", strip=True):
            value = metadata.find_previous("span", class_="SD")
            return value.get_text(strip=True) if value else None
    return None


def parse_ad_positions_html(html_dir: Path):
    records = []
    for source in sorted(html_dir.rglob("EG-AD-2.*.html")):
        match = re.fullmatch(r"EG-AD-2\.([A-Z0-9]{4})-en-GB\.html", source.name.upper())
        if not match:
            continue
        icao = match.group(1)
        soup = BeautifulSoup(source.read_text(encoding="utf-8", errors="replace"), "lxml")
        pairs = []
        heading = soup.find(string=re.compile(r"AERODROME REFERENCE POINT|\bARP\b", re.I))
        if heading:
            container = heading.find_parent(["div", "table", "tr"])
            if container:
                lat, lon = tagged_value(container, "GEO_LAT"), tagged_value(container, "GEO_LONG")
                if lat and lon:
                    pairs.append((lat, lon))
        if not pairs:
            lat_value = None
            for metadata in soup.find_all("span", class_="sdParams"):
                raw = metadata.get_text(" ", strip=True)
                value = metadata.find_previous("span", class_="SD")
                text = value.get_text(strip=True) if value else None
                if ";GEO_LAT;" in raw:
                    lat_value = text
                elif ";GEO_LONG;" in raw and lat_value and text:
                    pairs.append((lat_value, text))
                    lat_value = None
        for lat_text, lon_text in pairs:
            try:
                lat, lon = dms_to_decimal(lat_text, True), dms_to_decimal(lon_text, False)
            except ValueError:
                continue
            title = soup.find("title")
            name = title.get_text(" ", strip=True) if title else icao
            name = re.sub(r"\s+", " ", name)
            records.append({"icao": icao, "lat": lat, "lon": lon, "name": name})
            break
    return records


def build_ad_positions(html_dir: Path, output_nav: Path):
    records = parse_ad_positions_html(html_dir)
    if not records:
        raise RuntimeError("No valid aerodrome reference points extracted.")
    output_nav.mkdir(parents=True, exist_ok=True)
    lines = [f"{x['icao']} {x['lat']:.6f},{x['lon']:.6f} {x['name']}" for x in records]
    lines.append(str(len(lines)))
    (output_nav / "AD-positions").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"Wrote {len(records)} aerodrome positions")


def build_ad_entry_exit(html_dir: Path, output_nav: Path, airac_cycle: str):
    output_nav.mkdir(parents=True, exist_ok=True)
    (output_nav / "AD-entry-exit").write_text(f"# AD-entry-exit generated from UK eAIP AD 2, cycle {airac_cycle}\n", encoding="utf-8")


def gateway_json(path: str):
    with urlopen(request_url(f"{GATEWAY_API_BASE}/{path.lstrip('/')}"), timeout=60) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def gateway_items(airport: dict):
    for key in ("scenery", "sceneries", "sceneryPacks", "Scenery", "scenery_list", "scenery_list_items"):
        if isinstance(airport.get(key), list):
            return airport[key]
    for wrapper in ("data", "airport"):
        value = airport.get(wrapper)
        if isinstance(value, dict):
            for key in ("scenery", "sceneries", "sceneryPacks", "Scenery", "scenery_list", "scenery_list_items"):
                if isinstance(value.get(key), list):
                    return value[key]
    return []


def gateway_identifier(item: dict):
    for key in ("sceneryId", "id", "SceneryID", "scenery_id", "sceneryIdStr", "idStr"):
        if item.get(key) is not None:
            return str(item[key])
    return None


def download_gateway_airport(icao: str, destination: Path):
    try:
        airport = gateway_json(f"airport/{icao}")
    except Exception as error:
        log(f"Gateway skipped {icao}: {error}")
        return False
    items = [x for x in gateway_items(airport) if gateway_identifier(x)]
    if not items:
        return False
    items.sort(key=lambda x: str(x.get("dateApproved", x.get("updatedAt", ""))), reverse=True)
    scenery_id = gateway_identifier(items[0])
    urls = [f"{GATEWAY_WEB_BASE}/airports/{icao}/scenery/{scenery_id}/download", f"{GATEWAY_WEB_BASE}/airports/{icao}/scenery/{scenery_id}/download/zip"]
    for url in urls:
        try:
            with urlopen(request_url(url), timeout=120) as response:
                data = response.read()
            destination.write_bytes(data)
            return True
        except Exception:
            continue
    return False


def gateway_airport_codes():
    for path in ("airports", "airport"):
        try:
            data = gateway_json(path)
            values = data.get("airports", data) if isinstance(data, dict) else data
            if isinstance(values, list):
                return sorted({str(x.get("icao", "")).upper() for x in values if isinstance(x, dict) and str(x.get("icao", "")).upper().startswith(REGION)})
        except Exception:
            continue
    return []


def create_release(airac: str, output_dir: Path):
    release = output_dir / f"SkyRadar-AIRAC{airac}"
    if release.exists():
        shutil.rmtree(release)
    config = release / "CONFIG"
    nav = config / "nav"
    ad = config / "ad"
    bg = config / "bg"
    nav.mkdir(parents=True)
    ad.mkdir(parents=True)
    bg.mkdir(parents=True)
    cycle, zip_url = find_current_nats_airac()
    log(f"Detected current NATS AIRAC: {cycle}")
    with tempfile.TemporaryDirectory(prefix="skyradar-airac-") as temporary:
        temporary = Path(temporary)
        zip_path = temporary / "nats-airac.zip"
        html_path = temporary / "html"
        log(f"Downloading AIRAC ZIP: {zip_url}")
        download_file(zip_url, zip_path)
        log("Extracting AIRAC archive")
        with ZipFile(zip_path) as archive:
            archive.extractall(html_path)
        build_navaids_from_html(html_path, nav, airac)
        build_fixes_from_html(html_path, nav)
        build_airways_from_html(html_path, nav, airac)
        build_ad_positions(html_path, nav)
        build_ad_entry_exit(html_path, nav, airac)
    codes = gateway_airport_codes()
    log(f"Found {len(codes)} Gateway airport codes")
    downloaded = 0
    for icao in codes:
        if download_gateway_airport(icao, ad / f"{icao}.dat"):
            downloaded += 1
    manifest = {"airac_cycle": airac, "nats_cycle_detected": cycle, "built_at_utc": datetime.now(timezone.utc).isoformat(), "sources": {"nats_aip": NATS_AIP_URL, "gateway": GATEWAY_WEB_BASE}, "gateway_airports_downloaded": downloaded}
    (release / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    log(f"Release complete: {release}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--airac", required=True, help="Release folder cycle, e.g. 2609")
    parser.add_argument("--output-dir", type=Path, default=Path("."))
    args = parser.parse_args()
    create_release(args.airac, args.output_dir.resolve())


if __name__ == "__main__":
    main()
