#!/usr/bin/env python3
"""Build a self-contained SkyRadar AIRAC release from the NATS UK eAIP.

The generated directory is suitable for review and manual Git commit:

    SkyRadar-AIRAC2609/
      CONFIG/
        nav/
        ad/
        bg/
      manifest.json

The script never commits or pushes to GitHub.

Usage:
    python3 build_airac_release.py --airac 2609

Dependencies:
    pip3 install beautifulsoup4 lxml
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
import warnings
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from zipfile import ZipFile

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

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
SD_PAIR_RE = re.compile(
    r'<span[^>]*class="SD"[^>]*>(?P<val>.*?)</span>\s*'
    r'<span[^>]*class="sdParams"[^>]*>(?P<meta>.*?)</span>',
    re.DOTALL | re.IGNORECASE,
)
BAD_NAVPOINTS = {
    "AD", "AIP", "AIR", "AIRCRAFT", "AIRWAY", "AIRWAYS", "AIS", "AND",
    "APP", "APPROACH", "ARRIVAL", "ARRIVALS", "AT", "ATC", "ATIS", "ATS",
    "CAT", "CTR", "DCT", "DEPART", "DEPARTURE", "DEPARTURES", "DME", "EAST",
    "EMAIL", "ENR", "ENTRY", "EXIT", "FIR", "FL", "FOR", "FROM", "FT", "GA",
    "GROUND", "IFR", "INBOUND", "MET", "NATS", "NDB", "NM", "NORTH", "NOTAM",
    "ONE", "POINT", "PPR", "ROUTE", "ROUTES", "RUNWAY", "RWY", "SID", "SIDS",
    "SOUTH", "STAR", "STARS", "TMA", "THE", "THEN", "THREE", "TO", "TRACK",
    "TWO", "UK", "UNTIL", "VFR", "VIA", "VOR", "WEST", "WITH",
}


def log(message: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {message}", flush=True)


def request_url(url: str) -> Request:
    return Request(url, headers={"User-Agent": "SkyRadar AIRAC Release Builder/1.0"})


def fetch_text(url: str, timeout: int = 60) -> str:
    with urlopen(request_url(url), timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def download_file(url: str, destination: Path, timeout: int = 180) -> None:
    with urlopen(request_url(url), timeout=timeout) as response, destination.open("wb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)


def find_current_nats_airac() -> tuple[str, str]:
    """Use the same AIRAC ZIP discovery logic as the current updater."""
    page = fetch_text(NATS_AIP_URL, timeout=30)
    cycle = re.search(r"AIRAC\s+(\d{2})\s*/\s*(\d{4})", page, re.I)
    archive = re.search(
        r'href=["\']([^"\']*AIRAC-\d{2}-\d{2}\.zip[^"\']*)["\']',
        page,
        re.I,
    )
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
        raise ValueError("Invalid coordinate: %s" % text)
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
        if upper is None or lower is None:
            return None
        if first(fields, "UOM_DIST_VER_UPPER") != "FL":
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


def build_airways_from_html(html_dir: Path, output_nav: Path, airac_cycle: str) -> int:
    candidates = sorted(html_dir.rglob("EG-ENR-3.2-en-GB.html"))
    if not candidates:
        candidates = sorted(html_dir.rglob("*ENR-3.2*.html"))
    if not candidates:
        candidates = sorted(html_dir.rglob("*ENR-3-2*.html"))
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
            row_class = row.get("class") or []
            if "Table-row-type-2" in row_class:
                points.append(point_identity(row))
            elif "Table-row-type-3" in row_class:
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
            if top <= HIGH_LOW_BOUNDARY:
                bands = [(1, base, top)]
            elif base >= HIGH_LOW_BOUNDARY:
                bands = [(2, base, top)]
            else:
                bands = [(1, base, HIGH_LOW_BOUNDARY - 1), (2, HIGH_LOW_BOUNDARY, top)]

            for high_low, lower, upper in bands:
                lines.append(
                    f"{start_id} {REGION} {start_type} {end_id} {REGION} {end_type} "
                    f"N {high_low} {lower} {upper} {name}"
                )

    output_nav.mkdir(parents=True, exist_ok=True)
    output = output_nav / "awy.dat"
    with output.open("w", encoding="utf-8") as file:
        file.write("I\n")
        file.write(
            "1100 Version - data cycle %s, build %s, generated from UK eAIP ENR 3.2\n"
            % (airac_cycle, datetime.now().strftime("%Y%m%d"))
        )
        file.write("\n".join(lines))
        file.write("\n99\n")
    log(f"Built {len(lines)} airway segments, skipped {skipped}")
    return len(lines)


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


def build_fixes_from_html(html_dir: Path, output_nav: Path) -> int:
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
    (output_nav / "fix.dat").write_text(
        "".join("%.6f %.6f %s\n" % item for item in fixes),
        encoding="utf-8",
    )
    log(f"Wrote {len(fixes)} fixes")
    return len(fixes)


def parse_navaids_html(source: Path):
    soup = BeautifulSoup(source.read_text(encoding="utf-8", errors="replace"), "lxml")
    records, seen = [], set()

    for row in soup.find_all("tr", id=lambda value: bool(value and value.upper().startswith("NAV-"))):
        match = re.search(
            r"NAV-(VOR|DME|VORDME|NDB|ILSLOC|ILSGP|ILSDME)-([A-Z0-9]+)",
            row.get("id", ""),
            re.I,
        )
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

        records.append(
            {
                "type": NAV_TYPES.get(nav_kind, 2),
                "lat": lat,
                "lon": lon,
                "elev": elevation,
                "freq": frequency,
                "ident": ident,
                "name": fields.get("TXT_NAME", ident).strip() or ident,
            }
        )
        seen.add(ident)
    return records


def build_navaids_from_html(html_dir: Path, output_nav: Path, airac_cycle: str) -> int:
    candidates = sorted(html_dir.rglob("EG-ENR-4.1-en-GB.html"))
    if not candidates:
        candidates = sorted(html_dir.rglob("*ENR-4.1*.html"))
    if not candidates:
        raise FileNotFoundError("No ENR 4.1 navaids HTML found in extracted AIRAC.")

    records = parse_navaids_html(candidates[0])
    if not records:
        raise RuntimeError("No navaids extracted from ENR 4.1.")

    output_nav.mkdir(parents=True, exist_ok=True)
    lines = [
        "I",
        "1100 Version - data cycle %s, build %s, generated from UK eAIP ENR 4.1"
        % (airac_cycle, datetime.now().strftime("%Y%m%d")),
    ]
    for nav in records:
        lines.append(
            "%s %.6f %.6f %s %s 0 0 %s 0 %s %s"
            % (
                nav["type"], nav["lat"], nav["lon"], nav["elev"], nav["freq"],
                nav["ident"], REGION, nav["name"],
            )
        )
    lines.append("99")
    (output_nav / "navaid.dat").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"Wrote {len(records)} navaids")
    return len(records)


def tagged_value(container, field: str):
    for metadata in container.find_all("span", class_="sdParams"):
        if f";{field};" not in metadata.get_text(" ", strip=True):
            continue
        value = metadata.find_previous("span", class_="SD")
        return value.get_text(strip=True) if value else None
    return None


def airport_icao_from_path(path: Path):
    match = re.fullmatch(r"EG-AD-2\.([A-Z0-9]{4})-en-GB\.html", path.name.upper())
    return match.group(1) if match else None


def find_ad2_html_files(html_dir: Path):
    return sorted(
        path for path in html_dir.rglob("EG-AD-2.*.html")
        if airport_icao_from_path(path) is not None
    )


def parse_ad_positions_html(html_dir: Path):
    records = []

    for source in find_ad2_html_files(html_dir):
        icao = airport_icao_from_path(source)
        if not icao:
            continue

        soup = BeautifulSoup(source.read_text(encoding="utf-8", errors="replace"), "lxml")
        candidates = []

        # Same approach as the updater: prefer ARP area, then fall back to
        # paired GEO_LAT/GEO_LONG fields on the page.
        arp_heading = soup.find(string=re.compile(r"AERODROME REFERENCE POINT|\bARP\b", re.I))
        if arp_heading:
            container = arp_heading.find_parent(["tr", "table", "div"])
            if container:
                latitude = tagged_value(container, "GEO_LAT")
                longitude = tagged_value(container, "GEO_LONG")
                if latitude and longitude:
                    candidates.append((latitude, longitude))

        if not candidates:
            latitude = None
            for metadata in soup.find_all("span", class_="sdParams"):
                raw = metadata.get_text(" ", strip=True)
                value = metadata.find_previous("span", class_="SD")
                if value is None:
                    continue
                if ";GEO_LAT;" in raw:
                    latitude = value.get_text(strip=True)
                elif ";GEO_LONG;" in raw and latitude:
                    candidates.append((latitude, value.get_text(strip=True)))
                    latitude = None

        for latitude_text, longitude_text in candidates:
            try:
                latitude = dms_to_decimal(latitude_text, True)
                longitude = dms_to_decimal(longitude_text, False)
            except ValueError:
                continue

            title = soup.find("title")
            name = title.get_text(" ", strip=True) if title else icao
            name = re.sub(r"\s+", " ", name)
            name = re.sub(
                r"^.*?\b" + re.escape(icao) + r"\b\s*[-:]?\s*",
                "",
                name,
                flags=re.I,
            ).strip() or icao
            records.append({"icao": icao, "lat": latitude, "lon": longitude, "name": name})
            break

    return records


def build_ad_positions(html_dir: Path, output_nav: Path) -> int:
    records = parse_ad_positions_html(html_dir)
    if not records:
        raise RuntimeError(
            "No valid aerodrome reference points extracted. "
            "Expected EG-AD-2.<ICAO>-en-GB.html files below the eAIP directory."
        )

    output_nav.mkdir(parents=True, exist_ok=True)
    lines = [
        "%s %.6f,%.6f %s" % (record["icao"], record["lat"], record["lon"], record["name"])
        for record in records
    ]
    lines.append(str(len(records)))
    (output_nav / "AD-positions").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"Wrote {len(records)} aerodrome positions")
    return len(records)


def valid_navpoint(token: str, airport_icao: str) -> bool:
    token = token.upper().strip()
    return (
        token != airport_icao
        and token not in BAD_NAVPOINTS
        and bool(re.fullmatch(r"[A-Z][A-Z0-9]{1,5}", token))
    )


def build_ad_entry_exit(html_dir: Path, output_nav: Path, airac_cycle: str) -> int:
    lines, seen = [], set()

    for source in find_ad2_html_files(html_dir):
        airport = airport_icao_from_path(source)
        if not airport:
            continue
        soup = BeautifulSoup(source.read_text(encoding="utf-8", errors="replace"), "lxml")

        for row in soup.find_all("tr"):
            text = row.get_text(" ", strip=True).upper()
            if not text:
                continue
            if any(word in text for word in ("PPR", "NOTAM", "METAR", "ATIS", "TELEPHONE", "EMAIL", "FREQUENCY", "GROUND MOVEMENT", "PARKING", "FUEL", "HANDLING")):
                continue
            arrival = any(word in text for word in ("STAR", "ARRIVAL", "INBOUND"))
            departure = any(word in text for word in ("SID", "DEPARTURE", "TAKE-OFF", "TAKEOFF", "VIA"))
            if not (arrival or departure):
                continue
            tokens = [
                token for token in re.findall(r"\b[A-Z][A-Z0-9]{1,5}\b", text)
                if valid_navpoint(token, airport)
            ]
            if not tokens:
                continue
            line = "%s %s %s %s" % (
                airport,
                "exit" if departure and not arrival else "entry",
                tokens[-1] if departure and not arrival else tokens[0],
                "SID" if departure and not arrival else "STAR",
            )
            if line not in seen:
                seen.add(line)
                lines.append(line)

        if airport == "EGLL":
            for point in ("LAM", "BIG", "BNN", "OCK"):
                line = f"EGLL entry {point} STAR"
                if line not in seen:
                    seen.add(line)
                    lines.append(line)

    output_nav.mkdir(parents=True, exist_ok=True)
    output = output_nav / "AD-entry-exit"
    with output.open("w", encoding="utf-8") as file:
        file.write("# AD-entry-exit generated from UK eAIP AD 2, cycle %s, build %s\n" % (airac_cycle, datetime.now().strftime("%Y%m%d")))
        file.write("# Format: ICAO exit/entry fix [SID|STAR]\n")
        file.write("\n".join(lines))
        file.write("\n")
    log(f"Wrote {len(lines)} SID/STAR entry-exit records")
    return len(lines)


def gateway_json(path: str):
    url = "%s/%s" % (GATEWAY_API_BASE.rstrip("/"), path.lstrip("/"))
    with urlopen(request_url(url), timeout=60) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def gateway_scenery_items(airport: dict):
    for key in ("scenery", "sceneries", "sceneryPacks", "Scenery", "scenery_list", "scenery_list_items"):
        if isinstance(airport.get(key), list):
            return airport[key]
    for wrapper in ("data", "airport"):
        wrapped = airport.get(wrapper)
        if isinstance(wrapped, dict):
            for key in ("scenery", "sceneries", "sceneryPacks", "Scenery", "scenery_list", "scenery_list_items"):
                if isinstance(wrapped.get(key), list):
                    return wrapped[key]
    return []


def scenery_identifier(scenery: dict):
    for key in ("sceneryId", "id", "SceneryID", "scenery_id", "sceneryIdStr", "idStr"):
        if scenery.get(key) is not None:
            return str(scenery[key])
    return None


def write_gateway_layout(icao: str, output_path: Path) -> bool:
    try:
        airport = gateway_json("airport/%s" % icao)
    except HTTPError as error:
        if error.code == 404:
            return False
        raise
    except Exception as error:
        log(f"Gateway skipped {icao}: {error}")
        return False

    scenery = [item for item in gateway_scenery_items(airport) if scenery_identifier(item)]
    if not scenery:
        return False
    scenery.sort(key=lambda item: str(item.get("dateApproved") or item.get("updatedAt") or item.get("createdAt") or ""), reverse=True)
    scenery_id = scenery_identifier(scenery[0])

    url = f"{GATEWAY_WEB_BASE}/airports/{icao}/scenery/{scenery_id}/download"
    try:
        download_file(url, output_path, timeout=120)
        return True
    except Exception as error:
        log(f"Gateway download failed for {icao}: {error}")
        return False


def gateway_airport_codes() -> list[str]:
    for path in ("airports", "airport"):
        try:
            data = gateway_json(path)
        except Exception:
            continue
        values = data.get("airports", data) if isinstance(data, dict) else data
        if isinstance(values, list):
            codes = {
                str(item.get("icao", "")).upper()
                for item in values
                if isinstance(item, dict) and str(item.get("icao", "")).upper().startswith(REGION)
            }
            if codes:
                return sorted(codes)
    return []


def locate_eaip_directory(extract_root: Path) -> Path:
    """Locate the directory containing EG-ENR and EG-AD HTML files."""
    direct = extract_root / "eAIP"
    if direct.is_dir():
        return direct

    for candidate in extract_root.rglob("eAIP"):
        if candidate.is_dir():
            return candidate

    if list(extract_root.rglob("EG-ENR-4.1-en-GB.html")):
        return extract_root

    raise RuntimeError("Could not locate the eAIP HTML directory in the NATS AIRAC ZIP.")


def create_release(airac: str, output_dir: Path) -> None:
    release_root = output_dir / f"SkyRadar-AIRAC{airac}"
    if release_root.exists():
        shutil.rmtree(release_root)

    nav_root = release_root / "CONFIG" / "nav"
    ad_root = release_root / "CONFIG" / "ad"
    bg_root = release_root / "CONFIG" / "bg"
    nav_root.mkdir(parents=True)
    ad_root.mkdir(parents=True)
    bg_root.mkdir(parents=True)

    nats_cycle, zip_url = find_current_nats_airac()
    log(f"Detected current NATS AIRAC: {nats_cycle}")
    log(f"Downloading AIRAC ZIP: {zip_url}")

    with tempfile.TemporaryDirectory(prefix="skyradar-airac-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        zip_path = temporary_root / "nats-airac.zip"
        extract_root = temporary_root / "html"
        extract_root.mkdir()

        download_file(zip_url, zip_path)
        log("Extracting AIRAC archive")
        with ZipFile(zip_path) as archive:
            archive.extractall(extract_root)

        eaip_root = locate_eaip_directory(extract_root)
        log(f"Using eAIP directory: {eaip_root}")

        navaid_count = build_navaids_from_html(eaip_root, nav_root, airac)
        fix_count = build_fixes_from_html(eaip_root, nav_root)
        airway_count = build_airways_from_html(eaip_root, nav_root, airac)
        aerodrome_count = build_ad_positions(eaip_root, nav_root)
        entry_exit_count = build_ad_entry_exit(eaip_root, nav_root, airac)

    codes = gateway_airport_codes()
    log(f"Found {len(codes)} EG airport codes on X-Plane Gateway")
    gateway_downloads = 0
    for icao in codes:
        if write_gateway_layout(icao, ad_root / f"{icao}.dat"):
            gateway_downloads += 1

    manifest = {
        "airac_cycle": airac,
        "nats_cycle_detected": nats_cycle,
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "sources": {"nats_aip": NATS_AIP_URL, "xplane_gateway": GATEWAY_WEB_BASE},
        "counts": {
            "navaids": navaid_count,
            "fixes": fix_count,
            "airway_segments": airway_count,
            "aerodrome_positions": aerodrome_count,
            "entry_exit_records": entry_exit_count,
            "gateway_layouts": gateway_downloads,
        },
    }
    (release_root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    log(f"Release complete: {release_root}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a standalone SkyRadar AIRAC release folder.")
    parser.add_argument("--airac", required=True, help="Release cycle, e.g. 2609")
    parser.add_argument("--output-dir", type=Path, default=Path("."), help="Parent directory for SkyRadar-AIRACxxxx")
    args = parser.parse_args()
    create_release(args.airac, args.output_dir.resolve())


if __name__ == "__main__":
    main()
