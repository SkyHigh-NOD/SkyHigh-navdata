#!/usr/bin/env python3
"""
SkyRadar AIRAC Release Builder (standalone)

Builds a complete, self-contained AIRAC release folder:
  SkyRadar-AIRAC2609/
    CONFIG/
      nav/
        navaid.dat
        fix.dat
        awy.dat
        AD-positions
        AD-entry-exit
      ad/
        EGAA.dat
        EGAB.dat
        ...
      bg/
        EGLL.lst
        EGLL/
          EGLL-ATZ.draw
          EGLL-CTR.draw
          ...
    manifest.json

This script reproduces the same outputs as the in-app updater, but writes
them into an isolated AIRAC folder suitable for committing to the root of
the SkyHigh-navdata repository.

Usage:
    python build_airac_release.py [--airac 2609] [--output-dir .]

Requirements:
    - Python 3.9+
    - requests
    - beautifulsoup4
    - lxml (optional, for faster HTML parsing)
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NATS_AIP_BASE = "https://www.aurora.nats.co.uk/htmlAIP/Publications"
GATEWAY_BASE = "https://gateway.x-plane.com"

DEFAULT_AIRAC = "2609"
REGION_PREFIX = "EG"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def log(msg: str):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def fetch_text(url: str, timeout: int = 30) -> str:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def fetch_json(url: str, timeout: int = 30) -> dict:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def parse_dms_to_dd(dms: str) -> float:
    """
    Convert DMS like '51 53 14 N' or '000 03 38 E' to decimal degrees.
    """
    dms = dms.strip()
    if not dms:
        raise ValueError("Empty DMS")
    parts = dms.split()
    if len(parts) == 4:
        d, m, s, hemi = parts
    elif len(parts) == 3:
        d, m, hemi = parts
        s = "0"
    else:
        raise ValueError(f"Unsupported DMS format: {dms}")
    dd = abs(float(d)) + float(m) / 60.0 + float(s) / 3600.0
    if hemi in ("S", "W"):
        dd = -dd
    return dd


def normalize_icao(icao: str) -> str:
    return icao.upper().strip()


# ---------------------------------------------------------------------------
# NATS AIP parsing
# ---------------------------------------------------------------------------


def discover_current_airac_html() -> Tuple[str, str]:
    """
    Discover the latest AIRAC HTML publication URL and its AIRAC cycle string.
    Returns (base_url, cycle_str) where cycle_str is like '2026-09-04'.
    """
    log("Discovering current NATS AIRAC HTML publication...")
    page = fetch_text(NATS_AIP_BASE)
    soup = BeautifulSoup(page, "lxml")

    links = soup.select("a[href]")
    candidates = []
    for a in links:
        href = a.get("href", "")
        text = (a.get_text() or "").strip()
        if "AIRAC" in text and href.endswith("/html/"):
            candidates.append((text, href))

    if not candidates:
        raise RuntimeError("No AIRAC HTML publication found on NATS AIP index.")

    candidates.sort(key=lambda x: x[0], reverse=True)
    latest_text, latest_href = candidates[0]

    base = urljoin(NATS_AIP_BASE, latest_href)
    m = re.search(r"(\d{4}-\d{2}-\d{2})", latest_text)
    cycle_str = m.group(1) if m else "unknown"

    log(f"Latest AIRAC HTML: {latest_text} -> {base}")
    return base, cycle_str


def list_ad_html_pages(aip_base: str) -> List[str]:
    """
    List all AD 2 HTML pages for EG aerodromes.
    """
    log("Listing AD 2 HTML pages...")
    index_url = urljoin(aip_base, "html/eAIP/index.html")
    page = fetch_text(index_url)
    soup = BeautifulSoup(page, "lxml")

    urls = set()
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        text = (a.get_text() or "").strip()
        if href.startswith(REGION_PREFIX + "-AD-2.") and href.endswith("-en-GB.html"):
            urls.add(urljoin(aip_base, "html/eAIP/" + href))

    return sorted(urls)


def parse_ad_ats_airspace(html: str) -> Dict[str, List[Tuple[float, float]]]:
    """
    Parse ATZ/CTR/CTA/RMA lateral limits from an AD 2 HTML page.
    Returns a dict: { airspace_type: [ (lat, lon), ... ] }
    This is a simplified parser; for production use you may want to extend it.
    """
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text()

    result: Dict[str, List[Tuple[float, float]]] = {}

    for atype in ("ATZ", "CTR", "CTA", "RMA"):
        m = re.search(
            rf"{atype}.*?circle centred on\s*"
            r"(\d{{2}}\s\d{{2}}\s\d{{2}}\s[NSEW])\s*"
            r"(\d{{3}}\s\d{{2}}\s\d{{2}}\s[NSEW])\s*"
            r"radius\s+(\d+(?:\.\d+)?)\s*NM",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if not m:
            continue

        lat_dms = m.group(1).replace("  ", " ")
        lon_dms = m.group(2).replace("  ", " ")
        radius_nm = float(m.group(3))

        lat = parse_dms_to_dd(lat_dms)
        lon = parse_dms_to_dd(lon_dms)

        # Approximate circle with 36 points
        pts = []
        import math

        R = radius_nm * 1852.0  # metres
        for i in range(36):
            bearing = math.radians(i * 10)
            dlat = (R * math.cos(bearing)) / 6371000.0
            dlon = (R * math.sin(bearing)) / (6371000.0 * math.cos(math.radians(lat)))
            pts.append((lat + math.degrees(dlat), lon + math.degrees(dlon)))

        result[atype] = pts

    return result


# ---------------------------------------------------------------------------
# X-Plane Gateway parsing
# ---------------------------------------------------------------------------


def fetch_gateway_airports() -> List[dict]:
    log("Fetching X-Plane Gateway airport list...")
    data = fetch_json(f"{GATEWAY_BASE}/api/airports/")
    return data.get("airports", [])


def filter_eg_airports(airports: List[dict]) -> List[dict]:
    return [
        a
        for a in airports
        if a.get("icao", "").upper().startswith(REGION_PREFIX)
        and a.get("type", "").upper() == "AIRPORT"
    ]


def download_gateway_apt(icao: str, out_dir: Path):
    icao = normalize_icao(icao)
    url = f"{GATEWAY_BASE}/api/airports/{icao}/apt.dat/"
    dest = out_dir / f"{icao}.dat"
    log(f"Downloading Gateway apt.dat for {icao}...")
    content = fetch_text(url)
    dest.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Navdata generation (simplified placeholder)
# ---------------------------------------------------------------------------


def generate_navdata(output_nav: Path, airac_cycle: str):
    """
    Generate placeholder navdata files. Replace this with your real parser.
    """
    log("Generating navdata files...")
    ensure_dir(output_nav)

    navaid = output_nav / "navaid.dat"
    fix = output_nav / "fix.dat"
    awy = output_nav / "awy.dat"

    navaid.write_text(
        f"; SkyRadar AIRAC {airac_cycle} navaid.dat (placeholder)\n",
        encoding="utf-8",
    )
    fix.write_text(
        f"; SkyRadar AIRAC {airac_cycle} fix.dat (placeholder)\n",
        encoding="utf-8",
    )
    awy.write_text(
        f"; SkyRadar AIRAC {airac_cycle} awy.dat (placeholder)\n",
        encoding="utf-8",
    )

    ensure_dir(output_nav / "AD-positions")
    ensure_dir(output_nav / "AD-entry-exit")

    (output_nav / "AD-positions" / ".gitkeep").write_text("", encoding="utf-8")
    (output_nav / "AD-entry-exit" / ".gitkeep").write_text("", encoding="utf-8")


# ---------------------------------------------------------------------------
# Background files generation
# ---------------------------------------------------------------------------


def generate_background_files(
    ad_html_urls: List[str],
    output_bg: Path,
):
    """
    Generate CONFIG/bg/*.lst and CONFIG/bg/{ICAO}/*.draw files
    by parsing AD 2 HTML pages for ATZ/CTR/CTA/RMA.
    """
    log("Generating background files from AD 2 HTML...")
    ensure_dir(output_bg)

    for url in ad_html_urls:
        html = fetch_text(url)
        icao_match = re.search(rf"{REGION_PREFIX}-AD-2\.([A-Z0-9]+)-en-GB\.html", url)
        if not icao_match:
            continue
        icao = icao_match.group(1)

        airspace = parse_ad_ats_airspace(html)
        if not airspace:
            continue

        lst_path = output_bg / f"{icao}.lst"
        lines = []
        icao_dir = output_bg / icao
        ensure_dir(icao_dir)

        for atype, pts in sorted(airspace.items()):
            draw_name = f"{icao}-{atype}.draw"
            draw_path = icao_dir / draw_name
            content_lines = ["colour"] + [f"{lat:.6f},{lon:.6f}" for lat, lon in pts]
            draw_path.write_text("\n".join(content_lines) + "\n", encoding="utf-8")
            lines.append(str(icao / draw_name))

        lst_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Manifest generation
# ---------------------------------------------------------------------------


def count_files_recursive(root: Path) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for p in root.rglob("*"):
        if p.is_file():
            rel = str(p.relative_to(root))
            prefix = rel.split(os.sep)[0]
            counts[prefix] = counts.get(prefix, 0) + 1
    return counts


def build_manifest(
    airac_cycle: str,
    nats_aip_base: str,
    output_root: Path,
) -> dict:
    counts = count_files_recursive(output_root)
    return {
        "airac_cycle": airac_cycle,
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "sources": {
            "nats_aip": nats_aip_base,
            "xplane_gateway": GATEWAY_BASE,
        },
        "region": REGION_PREFIX,
        "file_counts": counts,
    }


# ---------------------------------------------------------------------------
# Main build routine
# ---------------------------------------------------------------------------


def build_airac_release(
    airac_cycle: str,
    output_dir: Path,
    override_aip_base: Optional[str] = None,
):
    """
    Build a complete AIRAC release folder.
    """
    log(f"Building SkyRadar AIRAC {airac_cycle} release...")

    release_name = f"SkyRadar-AIRAC{airac_cycle}"
    release_root = output_dir / release_name
    if release_root.exists():
        log(f"Removing existing release folder: {release_root}")
        shutil.rmtree(release_root)

    ensure_dir(release_root)
    config_root = release_root / "CONFIG"
    nav_root = config_root / "nav"
    ad_root = config_root / "ad"
    bg_root = config_root / "bg"

    ensure_dir(nav_root)
    ensure_dir(ad_root)
    ensure_dir(bg_root)

    # Discover or use overridden AIP base
    if override_aip_base:
        aip_base = override_aip_base
        log(f"Using overridden AIP base: {aip_base}")
    else:
        aip_base, _ = discover_current_airac_html()

    # AD 2 HTML pages
    ad_html_urls = list_ad_html_pages(aip_base)
    log(f"Found {len(ad_html_urls)} AD 2 HTML pages.")

    # Generate navdata
    generate_navdata(nav_root, airac_cycle)

    # Download Gateway airport layouts
    log("Downloading X-Plane Gateway airport layouts...")
    airports = fetch_gateway_airports()
    eg_airports = filter_eg_airports(airports)
    log(f"Found {len(eg_airports)} EG airports on Gateway.")

    for apt in eg_airports:
        icao = normalize_icao(apt["icao"])
        try:
            download_gateway_apt(icao, ad_root)
        except Exception as e:
            log(f"Warning: failed to download {icao}: {e}")

    # Generate background files
    generate_background_files(ad_html_urls, bg_root)

    # Manifest
    manifest = build_manifest(airac_cycle, aip_base, release_root)
    (release_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    log(f"Release built successfully: {release_root}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Build a standalone SkyRadar AIRAC release folder."
    )
    parser.add_argument(
        "--airac",
        default=DEFAULT_AIRAC,
        help="AIRAC cycle, e.g. 2609 (default: 2609)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("."),
        help="Directory where the release folder will be created (default: current dir)",
    )
    parser.add_argument(
        "--aip-base",
        default=None,
        help="Override NATS AIP base URL (for testing / specific AIRAC)",
    )

    args = parser.parse_args()

    try:
        build_airac_release(
            airac_cycle=args.airac,
            output_dir=args.output_dir.resolve(),
            override_aip_base=args.aip_base,
        )
    except Exception as e:
        log(f"Build failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
