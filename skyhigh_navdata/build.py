"""Parse NATS UK eAIP HTML into the nav files SkyRadar reads.

Everything here is a straight port of the parsing/building logic that used to
live in SkyRadar's ``gui/dialogs/airac_update.py``. The only structural change
is that the output directory is now a parameter (``nav_dir``) instead of the
hard-coded ``CONFIG/nav`` next to the SkyRadar executable.
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

import warnings

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

HIGH_LOW_BOUNDARY = 245

LAT_RE = re.compile(r"^(\d{2})(\d{2})(\d{2}(?:\.\d+)?)([NS])$")
LON_RE = re.compile(r"^(\d{3})(\d{2})(\d{2}(?:\.\d+)?)([EW])$")
TAG_RE = re.compile(r"<[^>]+>")
POINT_TAGS = {"TDESIGNATED_POINT": "11", "TNDB": "2", "TVOR": "3", "TDME": "3"}
NAV_TYPES = {"VOR": 2, "VORDME": 2, "DME": 3, "NDB": 5, "ILSLOC": 4, "ILSGP": 12, "ILSDME": 3}
SD_PAIR_RE = re.compile(
    r'<span[^>]*class="SD"[^>]*>(?P<val>.*?)</span>\s*<span[^>]*class="sdParams"[^>]*>(?P<meta>.*?)</span>',
    re.DOTALL,
)
BAD_NAVPOINTS = {
    "AD", "AIP", "AIR", "AIRCRAFT", "AIRWAY", "AIRWAYS", "AIS", "AND", "APP", "APPROACH",
    "ARRIVAL", "ARRIVALS", "AT", "ATC", "ATIS", "ATS", "CAT", "CTR", "DCT", "DEPART",
    "DEPARTURE", "DEPARTURES", "DME", "EAST", "EMAIL", "ENR", "ENTRY", "EXIT", "FIR", "FL",
    "FOR", "FROM", "FT", "GA", "GROUND", "IFR", "INBOUND", "MET", "NATS", "NDB", "NM",
    "NORTH", "NOTAM", "ONE", "POINT", "PPR", "ROUTE", "ROUTES", "RUNWAY", "RWY", "SID",
    "SIDS", "SOUTH", "STAR", "STARS", "TMA", "THE", "THEN", "THREE", "TO", "TRACK", "TWO",
    "UK", "UNTIL", "VFR", "VIA", "VOR", "WEST", "WITH",
}


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", value)).strip()


def extract_entities(row):
    entities = defaultdict(lambda: defaultdict(list))
    for match in SD_PAIR_RE.finditer(str(row)):
        parts = match.group("meta").strip().split(";")
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
        upper_uom = first(fields, "UOM_DIST_VER_UPPER")
        lower_uom = first(fields, "UOM_DIST_VER_LOWER")
        if upper is None or lower is None or upper_uom != "FL":
            return None
        try:
            upper = float(upper)
            lower = float(lower)
        except ValueError:
            return None
        if lower_uom == "FT":
            lower /= 100
        elif lower_uom != "FL":
            return None
        return int(round(lower)), int(round(upper))
    return None


# --- Airways (ENR 3.2) ------------------------------------------------------

def build_airways_from_html(html_dir, nav_dir, icao_prefix, airac_cycle, log=None):
    candidates = sorted(Path(html_dir).rglob("EG-ENR-3.2-en-GB.html"))
    if not candidates:
        candidates = sorted(Path(html_dir).rglob("*ENR-3.2*.html"))
    if not candidates:
        candidates = sorted(Path(html_dir).rglob("*ENR-3-2*.html"))
    if not candidates:
        raise FileNotFoundError("No ENR 3.2 ATS routes HTML found in extracted AIRAC.")
    source = candidates[0]
    if log:
        log("Building airways from NATS ENR 3.2 HTML...")
        log("Using airways file: %s" % source.name)

    soup = BeautifulSoup(source.read_text(encoding="utf-8", errors="replace"), "lxml")
    lines = []
    skipped = 0
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
        points = []
        segments = []
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
            if top <= HIGH_LOW_BOUNDARY:
                bands = [(1, base, top)]
            elif base >= HIGH_LOW_BOUNDARY:
                bands = [(2, base, top)]
            else:
                bands = [(1, base, HIGH_LOW_BOUNDARY - 1), (2, HIGH_LOW_BOUNDARY, top)]
            for high_low, lower, upper in bands:
                lines.append(
                    f"{start_id} {icao_prefix} {start_type} {end_id} {icao_prefix} "
                    f"{end_type} N {high_low} {lower} {upper} {name}"
                )

    output = Path(nav_dir) / "awy.dat"
    text = "I\n"
    text += "1100 Version - data cycle %s, build %s, generated from UK eAIP ENR 3.2\n" % (
        airac_cycle,
        datetime.now().strftime("%Y%m%d"),
    )
    text += "\n".join(lines)
    text += "\n99\n"
    _atomic_write_text(output, text)
    if log:
        log("Built %d airway segments, skipped %d" % (len(lines), skipped))
        log("Wrote airways to %s" % output)
    return len(lines)


# --- Fixes (ENR 4.4) --------------------------------------------------------

def parse_fixes_html(source):
    soup = BeautifulSoup(source.read_text(encoding="utf-8", errors="replace"), "lxml")
    records = []
    seen = set()
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
            latitude = dms_to_decimal(coords[0].get_text(strip=True), True)
            longitude = dms_to_decimal(coords[1].get_text(strip=True), False)
        except ValueError:
            continue
        seen.add(ident)
        records.append((latitude, longitude, ident))
    return records


def build_fixes_from_html(html_dir, nav_dir, airac_cycle, log=None):
    candidates = sorted(Path(html_dir).rglob("*ENR-4.4*.html"))
    if not candidates:
        raise FileNotFoundError("No ENR 4.4 significant-points HTML found in extracted AIRAC.")
    fixes = []
    for source in candidates:
        if log:
            log("Trying fixes file: %s" % source.name)
        fixes = parse_fixes_html(source)
        if fixes:
            break
    if not fixes:
        raise RuntimeError("No fixes extracted from ENR 4.4; CONFIG/nav/fix.dat was not written.")
    output = Path(nav_dir) / "fix.dat"
    _atomic_write_text(output, "".join("%.6f %.6f %s\n" % item for item in fixes))
    if log:
        log("Wrote %d fixes to %s" % (len(fixes), output))
    return len(fixes)


# --- Navaids (ENR 4.1) ------------------------------------------------------

def parse_navaids_html(source):
    soup = BeautifulSoup(source.read_text(encoding="utf-8", errors="replace"), "lxml")
    records = []
    seen = set()
    for row in soup.find_all("tr", id=lambda value: bool(value and value.upper().startswith("NAV-"))):
        match = re.search(r"NAV-(VOR|DME|VORDME|NDB|ILSLOC|ILSGP|ILSDME)-([A-Z0-9]+)", row.get("id", ""), re.I)
        if not match:
            continue
        nav_kind, ident = match.groups()
        nav_kind = nav_kind.upper()
        ident = ident.upper()
        if ident in seen:
            continue
        fields = {}
        for span in row.find_all("span", class_="SD"):
            metadata = span.find_next_sibling("span", class_="sdParams")
            if metadata is None:
                continue
            raw = metadata.get_text(" ", strip=True)
            for field in ("VAL_FREQ", "GEO_LAT", "GEO_LONG", "VAL_ELEV", "TXT_NAME"):
                if ";%s;" % field in raw:
                    fields[field] = span.get_text(strip=True)
        if "GEO_LAT" not in fields or "GEO_LONG" not in fields:
            continue
        try:
            latitude = dms_to_decimal(fields["GEO_LAT"], True)
            longitude = dms_to_decimal(fields["GEO_LONG"], False)
        except ValueError:
            continue
        try:
            elevation = int(float(re.sub(r"[^0-9.-]", "", fields.get("VAL_ELEV", "0")) or 0))
        except ValueError:
            elevation = 0
        frequency = re.sub(r"[^0-9.]", "", fields.get("VAL_FREQ", "0")) or "0"
        if nav_kind in ("VOR", "VORDME") and "." not in frequency:
            frequency += ".000"
        records.append({
            "type": NAV_TYPES.get(nav_kind, 2),
            "lat": latitude,
            "lon": longitude,
            "elev": elevation,
            "freq": frequency,
            "ident": ident,
            "name": fields.get("TXT_NAME", ident).strip() or ident,
        })
        seen.add(ident)
    return records


def build_navaids_from_html(html_dir, nav_dir, icao_prefix, airac_cycle, log=None):
    candidates = sorted(Path(html_dir).rglob("EG-ENR-4.1-en-GB.html"))
    if not candidates:
        candidates = sorted(Path(html_dir).rglob("*ENR-4.1*.html"))
    if not candidates:
        raise FileNotFoundError("No ENR 4.1 navaids HTML found in extracted AIRAC.")
    source = candidates[0]
    if log:
        log("Using navaids file: %s" % source.name)
    records = parse_navaids_html(source)
    if not records:
        raise RuntimeError("No navaids extracted from ENR 4.1; CONFIG/nav/navaid.dat was not written.")
    output = Path(nav_dir) / "navaid.dat"
    text = "I\n"
    text += "1100 Version - data cycle %s, build %s, generated from UK eAIP ENR 4.1\n" % (
        airac_cycle,
        datetime.now().strftime("%Y%m%d"),
    )
    for nav in records:
        text += "%s %.6f %.6f %s %s 0 0 %s 0 %s %s\n" % (
            nav["type"], nav["lat"], nav["lon"], nav["elev"], nav["freq"],
            nav["ident"], icao_prefix, nav["name"],
        )
    text += "99\n"
    _atomic_write_text(output, text)
    if log:
        log("Wrote %d navaids to %s" % (len(records), output))
    return len(records)


# --- Aerodrome reference points (AD 2) --------------------------------------

def tagged_value(container, field):
    for metadata in container.find_all("span", class_="sdParams"):
        if ";%s;" % field not in metadata.get_text(" ", strip=True):
            continue
        value = metadata.find_previous("span", class_="SD")
        return value.get_text(strip=True) if value else None
    return None


# NATS has shipped this filename with both "EG-AD-2.EGSS-..." and
# "EG-AD-2_EGSS-..." separators (sometimes with a "-2" page suffix) across
# different AIRAC cycles - accept any of them.
AD2_FILENAME_RE = re.compile(r"([A-Z0-9]{2})-AD-2[._-]([A-Z0-9]{4})-en-GB(?:-\d+)?\.html", re.I)


def airport_icao_from_path(path) -> str | None:
    match = AD2_FILENAME_RE.fullmatch(Path(path).name)
    return match.group(2).upper() if match else None


def find_ad2_html_files(html_dir, icao_prefix="EG"):
    """Every AD 2 aerodrome HTML page for the region, sorted by path."""
    prefix = icao_prefix.upper()
    found = []
    for path in sorted(Path(html_dir).rglob("%s-AD-2*-en-GB*.html" % prefix)):
        match = AD2_FILENAME_RE.fullmatch(path.name)
        if match and match.group(1).upper() == prefix:
            found.append(path)
    return found


def parse_ad_positions_html(html_dir, icao_prefix):
    records = []
    for source in find_ad2_html_files(html_dir, icao_prefix):
        icao = airport_icao_from_path(source)
        if not icao:
            continue
        soup = BeautifulSoup(source.read_text(encoding="utf-8", errors="replace"), "lxml")
        candidates = []
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
            name = re.sub(r"^.*?\b" + re.escape(icao) + r"\b\s*[-:]?\s*", "", name, flags=re.I).strip() or icao
            records.append({"icao": icao, "lat": latitude, "lon": longitude, "name": name})
            break
    return records


def build_ad_positions_from_html(html_dir, nav_dir, icao_prefix, airac_cycle, log=None):
    records = parse_ad_positions_html(html_dir, icao_prefix)
    if not records:
        raise RuntimeError("No valid aerodrome reference points extracted; CONFIG/nav/AD-positions was not written.")
    output = Path(nav_dir) / "AD-positions"
    lines = ["%s %.6f,%.6f %s" % (record["icao"], record["lat"], record["lon"], record["name"]) for record in records]
    lines.append(str(len(records)))
    _atomic_write_text(output, "\n".join(lines) + "\n")
    if log:
        log("Wrote %d aerodrome positions to %s" % (len(records), output))
    return [record["icao"] for record in records]


# --- SID/STAR entry-exit points (AD 2) --------------------------------------

def build_ad_entry_exit_from_html(html_dir, nav_dir, icao_prefix, airac_cycle, log=None):
    if log:
        log("Building AD-entry-exit (SIDs/STARs) from NATS AD 2 HTML...")
    lines = []
    seen = set()
    for source in find_ad2_html_files(html_dir, icao_prefix):
        airport = airport_icao_from_path(source)
        if not airport:
            continue
        soup = BeautifulSoup(source.read_text(encoding="utf-8", errors="replace"), "lxml")
        count = 0
        for row in soup.find_all("tr"):
            text = row.get_text(" ", strip=True).upper()
            if not text or any(word in text for word in (
                "PPR", "NOTAM", "METAR", "ATIS", "TELEPHONE", "EMAIL", "FREQUENCY",
                "GROUND MOVEMENT", "PARKING", "FUEL", "HANDLING",
            )):
                continue
            arrival = any(word in text for word in ("STAR", "ARRIVAL", "INBOUND"))
            departure = any(word in text for word in ("SID", "DEPARTURE", "TAKE-OFF", "TAKEOFF", "VIA"))
            if not (arrival or departure):
                continue
            tokens = [token for token in re.findall(r"\b[A-Z][A-Z0-9]{1,5}\b", text)
                      if token != airport and token not in BAD_NAVPOINTS]
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
                count += 1
        if airport == "EGLL":
            for point in ("LAM", "BIG", "BNN", "OCK"):
                line = "%s entry %s STAR" % (airport, point)
                if line not in seen:
                    seen.add(line)
                    lines.append(line)
                    count += 1
        if log:
            log("Found %d valid SID/STAR entry-exit points in %s" % (count, airport))
    output = Path(nav_dir) / "AD-entry-exit"
    text = "# AD-entry-exit generated from UK eAIP AD 2, cycle %s, build %s\n" % (
        airac_cycle,
        datetime.now().strftime("%Y%m%d"),
    )
    text += "# Format: ICAO exit/entry fix [proc]\n"
    text += "\n".join(lines)
    text += "\n"
    _atomic_write_text(output, text)
    if log:
        log("Wrote %d valid SID/STAR entries to %s" % (len(lines), output))
    return len(lines)


# --- Default CTR (enroute) radar positions ----------------------------------

# SkyRadar's launcher lists the CTR positions the controller has saved locally
# (CONFIG/CTR-positions.conf) and starts from these shipped defaults for any
# code there is no local entry for - so a fresh install already has an enroute
# position to pick. The name matches the ENR 2.1 sector group the airspace
# builder writes (CONFIG/bg/ENR21*), i.e. the whole UK enroute is one position
# whose sectors are then ticked on the radar.
DEFAULT_CTR_POSITIONS = [
    ("ENR21", "52.5,-1.5", "UK enroute (ENR 2.1 sectors)"),
]

def build_ctr_positions(nav_dir, airac_cycle="", log=None):
    """Write the default CTR radar positions shipped with the navdata."""
    output = Path(nav_dir) / "CTR-positions"
    lines = [
        "# Default CTR (enroute) radar positions, cycle %s, build %s." % (
            airac_cycle,
            datetime.now().strftime("%Y%m%d"),
        ),
        "# Format: <location code><TAB><radar position point spec>",
        "# SkyRadar uses these for any code without a locally saved entry; a",
        "# position saved locally under the same name always takes precedence.",
    ]
    for code, spec, description in DEFAULT_CTR_POSITIONS:
        lines.append("# %s" % description)
        lines.append("%s\t%s" % (code, spec))
    _atomic_write_text(output, "\n".join(lines) + "\n")
    if log:
        log("Wrote %d default CTR position(s) to %s" % (len(DEFAULT_CTR_POSITIONS), output))
    return len(DEFAULT_CTR_POSITIONS)


# --- Orchestration ----------------------------------------------------------

def build_navdata_from_html(html_dir, nav_dir, icao_prefix="EG", airac_cycle="", log=None):
    """Build all ``CONFIG/nav`` files. Returns a counts dict + aerodrome ICAOs."""
    if icao_prefix.upper() != "EG":
        raise ValueError("The NATS UK eAIP source supports the EG region only.")
    nav_dir = Path(nav_dir)
    nav_dir.mkdir(parents=True, exist_ok=True)
    navaids = build_navaids_from_html(html_dir, nav_dir, icao_prefix, airac_cycle, log)
    fixes = build_fixes_from_html(html_dir, nav_dir, airac_cycle, log)
    airways = build_airways_from_html(html_dir, nav_dir, icao_prefix, airac_cycle, log)
    icaos = build_ad_positions_from_html(html_dir, nav_dir, icao_prefix, airac_cycle, log)
    entry_exit = build_ad_entry_exit_from_html(html_dir, nav_dir, icao_prefix, airac_cycle, log)
    ctr_positions = build_ctr_positions(nav_dir, airac_cycle, log)
    return {
        "counts": {
            "navaids": navaids,
            "fixes": fixes,
            "airway_segments": airways,
            "aerodromes": len(icaos),
            "entry_exit_points": entry_exit,
            "ctr_positions": ctr_positions,
        },
        "icaos": icaos,
    }
