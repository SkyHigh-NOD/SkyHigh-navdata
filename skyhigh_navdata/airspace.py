"""Generate SkyRadar's ``CONFIG/bg`` airspace drawings from the NATS UK eAIP.

ENR 2.1 (FIR/UIR/TMA/CTA) and AD 2.17 (local CTR/ATZ/TMZ) lay their data out
differently - ENR 2.1 stacks name, lateral limits, vertical limits and class as
paragraphs in one table cell; AD 2.17 spreads them across three columns instead
- but the underlying vertex/arc markup and the polygon maths are identical, so
that part lives here once.

Ported from the earlier "airspace" branch of the navdata builder.
"""
from __future__ import annotations

import math
import re
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

from .build import (
    SD_PAIR_RE,
    TAG_RE,
    airport_icao_from_path,
    clean,
    dms_to_decimal,
    find_ad2_html_files,
)

AIRSPACE_COLOUR = "turquoise"
EARTH_RADIUS_NM = 3440.065
# arcs (and full circles, e.g. an ATZ) get flattened to a polyline; one
# vertex per this many degrees of sweep
ARC_STEP_DEG = 1.0
CIRCLE_POINTS = int(round(360 / ARC_STEP_DEG))
# trailing tokens in an AD 2.17 airspace name that mark where the "type"
# starts, e.g. "LONDON STANSTED TMZ 1" -> place word "STANSTED", type "TMZ 1"
# - also used to keep the AIP's own abbreviations shouting when pretty-casing
# names for both airspace builders
AIRSPACE_TYPES = {
    "CTR", "CTA", "ATZ", "TMA", "TMZ", "RMA", "MATZ", "TIZ", "TIA", "MTA",
    "ADIZ", "FIZ", "FISZ", "AIAA", "TRA", "DANGER", "RESTRICTED",
    "PROHIBITED", "GVS", "FIR", "UIR", "OCA", "ACC", "APP",
}


def _log(log, message):
    if log:
        log(message)


def _build_date():
    return datetime.now().strftime("%Y%m%d")


def visible_text(node) -> str:
    """Node's text with the hidden sdParams spans collapsed - BeautifulSoup's
    get_text() ignores CSS, so 'display: none' spans have to be stripped
    explicitly rather than relied on to stay invisible."""
    html = re.sub(r'<span[^>]*class="sdParams"[^>]*>.*?</span>', "", str(node),
                  flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<br\s*/?>", " ", html)
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", html)).strip()


# --- Polygon maths ----------------------------------------------------------

def parse_coord(value, is_lat: bool):
    if not value:
        return None
    try:
        return dms_to_decimal(value, is_lat)
    except ValueError:
        return None


def bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def destination(lat, lon, brg, dist_nm):
    ang = dist_nm / EARTH_RADIUS_NM
    p1 = math.radians(lat)
    l1 = math.radians(lon)
    b = math.radians(brg)
    p2 = math.asin(math.sin(p1) * math.cos(ang) +
                   math.cos(p1) * math.sin(ang) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(ang) * math.cos(p1),
                         math.cos(ang) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def arc_points(clat, clon, radius_nm, start, end, clockwise):
    """Flatten an arc between two known boundary points into a polyline,
    not including the endpoints themselves (the caller supplies those)."""
    b1 = bearing(clat, clon, *start)
    b2 = bearing(clat, clon, *end)
    sweep = (b2 - b1) % 360 if clockwise else -((b1 - b2) % 360)
    steps = max(int(abs(sweep) / ARC_STEP_DEG), 1)
    return [destination(clat, clon, b1 + sweep * i / steps, radius_nm)
            for i in range(1, steps)]


def full_circle(clat, clon, radius_nm):
    return [destination(clat, clon, 360 * i / CIRCLE_POINTS, radius_nm)
            for i in range(CIRCLE_POINTS)]


def vertex_sequence(paragraph):
    """Walk a lateral-limits paragraph in document order, returning one dict
    per vertex entity. The prose between the spans is what tells us an arc
    is clockwise or anti-clockwise, so it's captured as we go. Works whether
    the entity is a boundary point, an arc (adds radius/centre fields to the
    point either side of it), or - as for an ATZ - a bare centre-and-radius
    with no boundary point of its own."""
    html = str(paragraph)
    vertices = []
    current = None
    last_end = 0
    for match in SD_PAIR_RE.finditer(html):
        parts = clean(match.group("meta")).split(";")
        if len(parts) != 3:
            continue
        table, field, entity_id = parts
        if table != "TAIRSPACE_VERTEX":
            continue
        gap = visible_text(html[last_end:match.start()])
        last_end = match.end()
        value = clean(match.group("val"))

        if current is None or current["id"] != entity_id:
            current = {"id": entity_id}
            vertices.append(current)
        if field.startswith("ANNOTATION"):
            continue
        current[field] = value
        if field == "VAL_RADIUS_ARC":
            current["clockwise"] = "anti-clockwise" not in gap.lower()
    return vertices


def boundary(paragraph, name, log=None):
    """Vertices -> list of (lat, lon). Arcs are expanded; a lone
    centre+radius with no boundary points of its own (a plain circle, e.g.
    an ATZ) is expanded into a full ring. The AIP's closing repeat of the
    first vertex is dropped so each point appears once."""
    verts = vertex_sequence(paragraph)
    resolved = []
    for vertex in verts:
        lat = parse_coord(vertex.get("GEO_LAT"), True)
        lon = parse_coord(vertex.get("GEO_LONG"), False)
        if lat is not None and lon is not None:
            resolved.append((lat, lon, vertex))

    if not resolved:
        for vertex in verts:
            if "VAL_RADIUS_ARC" not in vertex or "GEO_LAT_ARC" not in vertex:
                continue
            clat = parse_coord(vertex.get("GEO_LAT_ARC"), True)
            clon = parse_coord(vertex.get("GEO_LONG_ARC"), False)
            try:
                radius = float(vertex["VAL_RADIUS_ARC"])
            except (TypeError, ValueError):
                radius = None
            if None in (clat, clon, radius):
                continue
            if vertex.get("UOM_RADIUS_ARC", "NM") != "NM":
                _log(log, "WARNING: %s circle radius is in %s, skipping"
                     % (name, vertex.get("UOM_RADIUS_ARC")))
                continue
            return full_circle(clat, clon, radius)
        return []

    points = []
    for index, (lat, lon, vertex) in enumerate(resolved):
        points.append((lat, lon))
        if "VAL_RADIUS_ARC" not in vertex or index + 1 >= len(resolved):
            continue
        clat = parse_coord(vertex.get("GEO_LAT_ARC"), True)
        clon = parse_coord(vertex.get("GEO_LONG_ARC"), False)
        try:
            radius = float(vertex["VAL_RADIUS_ARC"])
        except (TypeError, ValueError):
            radius = None
        if clat is None or clon is None or radius is None:
            _log(log, "WARNING: %s arc after %s %s is incomplete, drawing it straight"
                 % (name, vertex.get("GEO_LAT"), vertex.get("GEO_LONG")))
            continue
        if vertex.get("UOM_RADIUS_ARC", "NM") != "NM":
            _log(log, "WARNING: %s arc radius is in %s, drawing it straight"
                 % (name, vertex.get("UOM_RADIUS_ARC")))
            continue
        nxt = resolved[index + 1]
        points.extend(arc_points(clat, clon, radius, (lat, lon),
                                 (nxt[0], nxt[1]), vertex.get("clockwise", True)))

    if len(points) > 2 and points[0] == points[-1]:
        points.pop()
    return points


# --- Output helpers ---------------------------------------------------------

def airspace_slug(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name.upper()).strip("-")
    return slug or "UNNAMED"


def airspace_pretty(words) -> str:
    """Title-case the AIP's shouting, keeping known airspace abbreviations
    (and any all-caps compass/UK/FL tokens) shouting."""
    keep = AIRSPACE_TYPES | {"UK", "FL", "NORTH", "SOUTH", "EAST", "WEST"}
    out = []
    for word in words:
        core = word.strip("(),")
        out.append(word if core in keep or core.isdigit() else word.title())
    return " ".join(out)


def write_draw_file(path: Path, header_lines, points):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\r\n") as file:
        for line in header_lines:
            file.write(f"# {line}\n")
        file.write(f"{AIRSPACE_COLOUR}\n")
        for lat, lon in points:
            file.write(f"{lat:.7f},{lon:.7f}\n")


def write_lst_file(path: Path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\r\n") as file:
        file.write("\n".join(entries))


# --- ENR 2.1: FIR / UIR / TMA / CTA -----------------------------------------

def enr_airspace_limits(cell):
    """The 'Upper limit:' / 'Lower limit:' / 'Class:' lines from an ENR 2.1
    cell, in AIP order. A few entries (the FIRs) stack several layers in one
    cell, so this returns a flat list rather than a single triple."""
    out = []
    for paragraph in cell.find_all("p"):
        if "TAIRSPACE_VERTEX;GEO_LAT;" in str(paragraph):
            continue
        text = visible_text(paragraph)
        for label in ("Upper limit:", "Lower limit:", "Class:"):
            if text.startswith(label):
                value = re.sub(r"\s+", " ", text[len(label):]).strip()
                value = re.sub(r"^FL\s+(\d)", r"FL\1", value)
                out.append(f"{label.rstrip(':')}: {value}")
                break
    return out


def build_enr_airspace_from_html(html_dir, bg_dir, airac_cycle="", log=None):
    candidates = sorted(Path(html_dir).rglob("EG-ENR-2.1-en-GB.html"))
    if not candidates:
        candidates = sorted(Path(html_dir).rglob("*ENR-2.1*.html"))
    if not candidates:
        candidates = sorted(Path(html_dir).rglob("*ENR-2-1*.html"))
    if not candidates:
        raise FileNotFoundError("No ENR 2.1 airspace HTML found in extracted AIRAC.")

    source = candidates[0]
    _log(log, "Using ENR 2.1 airspace file: %s" % source.name)
    soup = BeautifulSoup(source.read_text(encoding="utf-8", errors="replace"), "lxml")

    output_bg = Path(bg_dir)
    group_dir = output_bg / "ENR2-1"
    index, used, skipped, total_points = [], {}, 0, 0

    for table in soup.find_all("table"):
        for tbody in table.find_all("tbody", recursive=False):
            for row in tbody.find_all("tr", recursive=False):
                cell = row.find("td")
                if cell is None:
                    continue

                name = None
                for match in SD_PAIR_RE.finditer(str(cell)):
                    parts = clean(match.group("meta")).split(";")
                    if len(parts) == 3 and parts[0] == "TAIRSPACE" and parts[1] == "TXT_NAME":
                        name = clean(match.group("val"))
                        break
                if not name:
                    continue

                lateral = next((p for p in cell.find_all("p")
                                if "TAIRSPACE_VERTEX;GEO_LAT;" in str(p)), None)
                if lateral is None:
                    _log(log, "WARNING: %s has no lateral limits, skipping" % name)
                    skipped += 1
                    continue

                points = boundary(lateral, name, log)
                if len(points) < 3:
                    _log(log, "WARNING: %s resolved to %d point(s), skipping" % (name, len(points)))
                    skipped += 1
                    continue

                stem = airspace_slug(name)
                used[stem] = used.get(stem, 0) + 1
                if used[stem] > 1:
                    stem = f"{stem}-{used[stem]}"
                filename = f"{stem}.draw"

                header = [name, *enr_airspace_limits(cell)]
                header.append("Data cycle %s, build %s, generated from UK eAIP ENR 2.1"
                              % (airac_cycle, _build_date()))

                write_draw_file(group_dir / filename, header, points)
                total_points += len(points)
                index.append(f"ENR2-1/{filename}  DRAW  {airspace_pretty(name.split())}")

    if not index:
        raise RuntimeError("No airspace extracted from ENR 2.1.")

    write_lst_file(output_bg / "ENR2-1.lst", index)
    _log(log, "Wrote %d ENR 2.1 airspace file(s) (%d points), skipped %d"
         % (len(index), total_points, skipped))
    return len(index)


# --- AD 2.17: local CTR / ATZ / TMZ, one .lst per airport -------------------

def ad_split_type(name: str):
    """('STANSTED', ['CTR']) for 'LONDON STANSTED CTR' - the word right
    before the recognised type token(s), and the type token(s) themselves.
    Falls back to (whole name, []) if no known type token is found."""
    words = name.split()
    for index, word in enumerate(words):
        if word.rstrip(",") in AIRSPACE_TYPES:
            return (words[index - 1] if index > 0 else ""), words[index:]
    return name, []


def ad_vertical_limits(cell):
    """[(upper, lower), ...] in document order, from an AD 2.17 'Vertical
    Limits' column cell."""
    pairs, upper = [], None
    for paragraph in cell.find_all("p"):
        text = visible_text(paragraph)
        if text.startswith("Upper limit:"):
            upper = re.sub(r"^FL\s+(\d)", r"FL\1", text[len("Upper limit:"):].strip())
        elif text.startswith("Lower limit:") and upper is not None:
            lower = re.sub(r"^FL\s+(\d)", r"FL\1", text[len("Lower limit:"):].strip())
            pairs.append((upper, lower))
            upper = None
    return pairs


def ad_class_values(cell):
    """['D', ...] in document order, from an AD 2.17 'Airspace Class' column cell."""
    return [visible_text(p) for p in cell.find_all("p") if visible_text(p)]


def build_ad_local_airspace_from_html(html_dir, bg_dir, icao_prefix="EG", airac_cycle="", log=None):
    output_bg = Path(bg_dir)
    total_files = 0
    build_date = _build_date()

    for source in find_ad2_html_files(html_dir, icao_prefix):
        icao = airport_icao_from_path(source)
        if not icao:
            continue

        soup = BeautifulSoup(source.read_text(encoding="utf-8", errors="replace"), "lxml")
        section = soup.find("div", id=f"{icao}-AD-2.17")
        table = section.find("table") if section else None
        if table is None:
            continue  # this airport's AIP entry has no ATS-airspace table

        tbodies = table.find_all("tbody", recursive=False)
        if not tbodies:
            continue

        airport_dir = output_bg / icao
        index, used, skipped, total_points = [], {}, 0, 0

        for row in tbodies[0].find_all("tr", recursive=False):
            cells = row.find_all("td", recursive=False)
            if len(cells) < 3:
                continue

            name = None
            for match in SD_PAIR_RE.finditer(str(cells[0])):
                parts = clean(match.group("meta")).split(";")
                if len(parts) == 3 and parts[0] == "TAIRSPACE" and parts[1] == "TXT_NAME":
                    name = clean(match.group("val"))
                    break
            if not name:
                _log(log, "WARNING: %s has an AD 2.17 row with no name "
                     "(possible continuation row), skipping" % icao)
                skipped += 1
                continue

            lateral = next((p for p in cells[0].find_all("p") if "TAIRSPACE_VERTEX;" in str(p)), None)
            if lateral is None:
                _log(log, "WARNING: %s %s has no lateral limits, skipping" % (icao, name))
                skipped += 1
                continue

            points = boundary(lateral, f"{icao} {name}", log)
            if len(points) < 3:
                _log(log, "WARNING: %s %s resolved to %d point(s), skipping"
                     % (icao, name, len(points)))
                skipped += 1
                continue

            limit_pairs = ad_vertical_limits(cells[1])
            classes = ad_class_values(cells[2])
            if limit_pairs and classes and len(limit_pairs) != len(classes):
                _log(log, "WARNING: %s %s has %d vertical layer(s) but %d class value(s)"
                     % (icao, name, len(limit_pairs), len(classes)))

            place, type_words = ad_split_type(name)
            if type_words:
                stem = f"{icao}-{airspace_slug(' '.join(type_words))}"
                friendly = airspace_pretty(([place] if place else []) + type_words)
            else:
                stem = f"{icao}-{airspace_slug(name)}"
                friendly = airspace_pretty(name.split())

            used[stem] = used.get(stem, 0) + 1
            if used[stem] > 1:
                stem = f"{stem}-{used[stem]}"
            filename = f"{stem}.draw"

            header = [name]
            for index_, (upper, lower) in enumerate(limit_pairs):
                header.append(f"Upper limit: {upper}")
                header.append(f"Lower limit: {lower}")
                if index_ < len(classes):
                    header.append(f"Class: {classes[index_]}")
            if not limit_pairs:
                header.extend(f"Class: {value}" for value in classes)
            header.append("Data cycle %s, build %s, generated from UK eAIP %s AD 2.17"
                          % (airac_cycle, build_date, icao))

            write_draw_file(airport_dir / filename, header, points)
            total_points += len(points)
            index.append(f"{icao}/{filename}  DRAW  {friendly}")

        if not index:
            continue

        write_lst_file(output_bg / f"{icao}.lst", index)
        _log(log, "%s: wrote %d local airspace file(s) (%d points), skipped %d"
             % (icao, len(index), total_points, skipped))
        total_files += len(index)

    if total_files == 0:
        raise RuntimeError("No local airspace extracted from any airport's AD 2.17.")
    return total_files


# --- Orchestration ----------------------------------------------------------

def build_airspace_from_html(html_dir, bg_dir, icao_prefix="EG", airac_cycle="", log=None):
    """Build all ``CONFIG/bg`` drawings. Returns a counts dict."""
    if icao_prefix.upper() != "EG":
        raise ValueError("The NATS UK eAIP source supports the EG region only.")
    bg_dir = Path(bg_dir)
    bg_dir.mkdir(parents=True, exist_ok=True)
    enr = build_enr_airspace_from_html(html_dir, bg_dir, airac_cycle, log)
    ad = build_ad_local_airspace_from_html(html_dir, bg_dir, icao_prefix, airac_cycle, log)
    return {"enr_airspace": enr, "ad_airspace": ad}
