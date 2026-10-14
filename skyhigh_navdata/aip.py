"""Download the upstream sources: NATS UK AIRAC archive and X-Plane Gateway layouts."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

NATS_AIP_URL = "https://nats-uk.ead-it.com/cms-nats/opencms/en/Publications/AIP/"
GATEWAY_API_BASE = "https://gateway.x-plane.com/apiv1"
GATEWAY_WEB_BASE = "https://gateway.x-plane.com"

USER_AGENT = "SkyRadar navdata builder"


def request_url(url: str) -> Request:
    return Request(url, headers={"User-Agent": USER_AGENT})


def download_file(url: str, destination: Path, timeout: int = 120) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urlopen(request_url(url), timeout=timeout) as response, destination.open("wb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)


# --- NATS UK eAIP -----------------------------------------------------------

def find_current_nats_airac():
    """Return (cycle "MM/YY", zip_url) for the current NATS UK AIRAC archive."""
    with urlopen(request_url(NATS_AIP_URL), timeout=30) as response:
        page = response.read().decode("utf-8", errors="replace")
    cycle = re.search(r"AIRAC\s+(\d{2})\s*/\s*(\d{4})", page, re.I)
    archive = re.search(r'href=["\']([^"\']*AIRAC-\d{2}-\d{2}\.zip[^"\']*)["\']', page, re.I)
    if not cycle or not archive:
        raise RuntimeError("Could not find the current NATS AIRAC ZIP download link.")
    return "%s/%s" % (cycle.group(1), cycle.group(2)[-2:]), urljoin(NATS_AIP_URL, archive.group(1))


def download_and_extract_airac(dest_dir: Path, log=None) -> tuple[str, Path]:
    """Download the current NATS AIRAC ZIP and extract it. Returns (cycle, html_dir)."""
    cycle, zip_url = find_current_nats_airac()
    if log:
        log("Detected AIRAC cycle: %s" % cycle)
        log("Downloading AIRAC ZIP from NATS: %s" % zip_url)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    html_dir = dest_dir / "html"
    archive_path = dest_dir / "nats-airac.zip"
    download_file(zip_url, archive_path)
    if html_dir.exists():
        shutil.rmtree(html_dir)
    html_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(html_dir)
    if log:
        log("Extracted AIRAC archive to %s" % html_dir)
    return cycle, html_dir


# --- X-Plane Gateway airport layouts ---------------------------------------

def fetch_gateway_json(path: str, timeout: int = 60):
    url = "%s/%s" % (GATEWAY_API_BASE.rstrip("/"), path.lstrip("/"))
    with urlopen(request_url(url), timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def gateway_scenery_items(airport):
    list_keys = (
        "scenery", "sceneries", "sceneryPacks", "Scenery",
        "scenery_list", "scenery_list_items",
    )
    for key in list_keys:
        value = airport.get(key)
        if isinstance(value, list):
            return value
    for wrapper in ("data", "airport"):
        wrapped = airport.get(wrapper)
        if not isinstance(wrapped, dict):
            continue
        for key in list_keys:
            value = wrapped.get(key)
            if isinstance(value, list):
                return value
    return []


def scenery_identifier(scenery):
    for key in ("sceneryId", "id", "SceneryID", "scenery_id", "sceneryIdStr", "idStr"):
        value = scenery.get(key)
        if value is not None:
            return str(value)
    return None


def scenery_sort_key(scenery):
    for key in ("dateApproved", "approvedAt", "createdAt", "updatedAt", "dateCreated"):
        value = scenery.get(key)
        if value:
            return str(value)
    return ""


def fetch_gateway_airport(icao: str):
    return fetch_gateway_json("airport/%s" % icao.upper(), timeout=30)


def write_gateway_layout(icao: str, output_path: Path, log=None) -> bool:
    try:
        airport = fetch_gateway_airport(icao)
    except HTTPError as error:
        if error.code == 404:
            if log:
                log("No X-Plane Gateway airport found for %s." % icao)
            return False
        raise

    scenery = [item for item in gateway_scenery_items(airport) if scenery_identifier(item)]
    if not scenery:
        if log:
            log("No scenery packs found for %s in Gateway response." % icao)
        return False

    scenery.sort(key=scenery_sort_key, reverse=True)
    scenery_id = scenery_identifier(scenery[0])
    if log:
        log("Downloading Gateway scenery %s for %s..." % (scenery_id, icao))

    # https://gateway.x-plane.com/airports/{ICAO}/scenery/{id}/download
    download_url = "%s/airports/%s/scenery/%s/download" % (
        GATEWAY_WEB_BASE.rstrip("/"), icao.upper(), scenery_id,
    )

    with tempfile.TemporaryDirectory(prefix="skyradar-gateway-") as directory:
        archive_path = Path(directory) / (icao + ".zip")
        download_file(download_url, archive_path, timeout=120)
        try:
            with zipfile.ZipFile(archive_path) as archive:
                all_apts = [name for name in archive.namelist() if Path(name).name.lower() == "apt.dat"]
                if not all_apts:
                    if log:
                        log("Gateway ZIP for %s contains no apt.dat." % icao)
                    return False
                earth_nav_apts = [name for name in all_apts
                                  if "earth nav data" in name.replace("\\", "/").lower()]
                preferred = min(earth_nav_apts or all_apts,
                                key=lambda path: path.replace("\\", "/").count("/"))
                content = archive.read(preferred)
        except zipfile.BadZipFile as error:
            if log:
                log("Gateway download for %s scenery %s is not a valid ZIP: %s" % (icao, scenery_id, error))
            return False
        except HTTPError as error:
            if log:
                log("HTTP %s while downloading Gateway scenery %s for %s" % (error.code, scenery_id, icao))
            return False

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + ".tmp")
    temporary.write_bytes(content)
    os.replace(temporary, output_path)
    if log:
        log("Wrote airport layout: %s" % output_path)
    return True


def download_all_ad_layouts(icaos, ad_dir, log=None, skip=None) -> dict:
    """Download one apt.dat per aerodrome into ``ad_dir``. Returns a counts summary.

    ``skip`` is an optional collection of ICAOs that already have a local
    override; those are left for the caller to supply instead of downloading.
    """
    ad_dir = Path(ad_dir)
    ad_dir.mkdir(parents=True, exist_ok=True)
    skip = {code.upper() for code in skip} if skip else set()
    downloaded = unavailable = failed = custom = 0
    layouts = []
    for index, icao in enumerate(icaos, start=1):
        if icao.upper() in skip:
            custom += 1
            if log:
                log("[%d/%d] X-Plane Gateway layout: %s (local override)" % (index, len(icaos), icao))
            continue
        output = ad_dir / (icao.upper() + ".dat")
        if log:
            log("[%d/%d] X-Plane Gateway layout: %s" % (index, len(icaos), icao))
        try:
            if write_gateway_layout(icao, output, log):
                downloaded += 1
                layouts.append(icao.upper())
            else:
                unavailable += 1
        except Exception as error:  # noqa: BLE001 - keep going on individual failures
            failed += 1
            if log:
                log("Failed to download %s layout: %s" % (icao, error))
        time.sleep(0.25)
    if log:
        log("Airport layouts complete: %d downloaded, %d overridden, %d unavailable, %d failed."
            % (downloaded, custom, unavailable, failed))
    return {"downloaded": downloaded, "custom": custom, "unavailable": unavailable,
            "failed": failed, "layouts": layouts}
