"""Download the upstream sources: NATS UK AIRAC archive and X-Plane Gateway layouts."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from .pack import display_path, error_text, safe_extract

NATS_AIP_URL = "https://nats-uk.ead-it.com/cms-nats/opencms/en/Publications/AIP/"
GATEWAY_API_BASE = "https://gateway.x-plane.com/apiv1"
GATEWAY_WEB_BASE = "https://gateway.x-plane.com"

USER_AGENT = "SkyRadar navdata builder"


def request_url(url: str) -> Request:
    return Request(url, headers={"User-Agent": USER_AGENT})


def download_file(url: str, destination: Path, timeout: int = 120,
                  progress_cb=None) -> None:
    """Stream a URL to disk. ``progress_cb(bytes_done, bytes_total)`` fires per
    chunk when supplied. When the server omits Content-Length on the GET, a
    HEAD request fills the gap for the progress display; 0 means genuinely
    unknown. Nobody reads the total without a callback, so no HEAD is sent
    then.

    The body goes to a ``.part`` file that only takes the destination's name
    once it is complete, so an interrupted download never looks like a file.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    try:
        _stream_to(url, partial, timeout, progress_cb)
        os.replace(partial, destination)
    finally:
        if partial.exists():
            partial.unlink()


def _stream_to(url: str, partial: Path, timeout: int, progress_cb) -> None:
    with urlopen(request_url(url), timeout=timeout) as response, partial.open("wb") as output:
        total_header = response.headers.get("Content-Length")
        total = int(total_header) if total_header and total_header.isdigit() else 0
        declared = total
        if not total and progress_cb:
            try:
                with urlopen(Request(url, headers={"User-Agent": USER_AGENT},
                                     method="HEAD"), timeout=30) as head:
                    head_length = head.headers.get("Content-Length")
                    if head_length and head_length.isdigit():
                        total = int(head_length)
            except (OSError, HTTPException):
                pass  # the total only feeds the progress display
        done = 0
        while True:
            chunk = response.read(512 * 1024)
            if not chunk:
                break
            output.write(chunk)
            done += len(chunk)
            if progress_cb:
                progress_cb(done, total)
        if declared and done < declared:
            raise OSError("download stopped at %d of %d bytes" % (done, declared))


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


def archive_source_path(archive_path: Path) -> Path:
    """Where the URL a cached archive was downloaded from is recorded."""
    archive_path = Path(archive_path)
    return archive_path.with_name(archive_path.name + ".source")


def cached_archive_usable(archive_path: Path, zip_url: str) -> bool:
    """True when the cached archive is complete and is the one at ``zip_url``.

    The NATS link names the cycle (``AIRAC-10-26.zip``), so an archive whose
    recorded URL differs holds another cycle's data. One with no record, or
    one that does not open as a ZIP, is not trusted either.
    """
    archive_path = Path(archive_path)
    source_note = archive_source_path(archive_path)
    if not archive_path.is_file() or not source_note.is_file():
        return False
    try:
        if source_note.read_text(encoding="utf-8").strip() != zip_url:
            return False
        with zipfile.ZipFile(archive_path) as archive:
            return bool(archive.namelist())
    except (zipfile.BadZipFile, OSError):
        return False


def download_and_extract_airac(dest_dir: Path, log=None, progress_cb=None) -> tuple[str, Path]:
    """Download the current NATS AIRAC ZIP and extract it. Returns (cycle, html_dir).

    ``progress_cb(bytes_done, bytes_total)`` tracks the download when supplied.
    """
    cycle, zip_url = find_current_nats_airac()
    if log:
        log("Detected AIRAC cycle: %s" % cycle)
        log("Downloading AIRAC ZIP from NATS: %s" % zip_url)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    html_dir = dest_dir / "html"
    archive_path = dest_dir / "nats-airac.zip"

    if cached_archive_usable(archive_path, zip_url):
        if log:
            log("Found the NATS ZIP for this cycle (%.0f MB) - reusing it, no download."
                % (archive_path.stat().st_size / 1e6))
    else:
        if archive_path.exists():
            if log:
                log("The cached NATS ZIP is incomplete or from another cycle - downloading again.")
            archive_path.unlink()
        source_note = archive_source_path(archive_path)
        if source_note.exists():
            source_note.unlink()
        download_file(zip_url, archive_path, progress_cb=progress_cb)
        source_note.write_text(zip_url + "\n", encoding="utf-8")
    if html_dir.exists():
        shutil.rmtree(html_dir)
    html_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        safe_extract(archive, html_dir)
    if log:
        log("Extracted AIRAC archive to %s" % display_path(html_dir))
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
                log("Gateway download for %s scenery %s is not a valid ZIP: %s" % (icao, scenery_id, error_text(error)))
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
        log("Wrote airport layout: %s" % output_path.name)
    return True


def download_all_ad_layouts(icaos, ad_dir, log=None, skip=None, progress_cb=None) -> dict:
    """Download one apt.dat per aerodrome into ``ad_dir``. Returns a counts summary.

    ``skip`` is an optional collection of ICAOs that already have a local
    override; those are left for the caller to supply instead of downloading.
    ``progress_cb(done, total, icao)`` is called per aerodrome when supplied.
    """
    ad_dir = Path(ad_dir)
    ad_dir.mkdir(parents=True, exist_ok=True)
    skip = {code.upper() for code in skip} if skip else set()
    downloaded = unavailable = failed = custom = 0
    layouts = []
    total = len(icaos)
    for index, icao in enumerate(icaos, start=1):
        if icao.upper() in skip:
            custom += 1
            if log:
                log("[%d/%d] X-Plane Gateway layout: %s (local override)" % (index, total, icao))
        else:
            output = ad_dir / (icao.upper() + ".dat")
            if log:
                log("[%d/%d] X-Plane Gateway layout: %s" % (index, total, icao))
            try:
                if write_gateway_layout(icao, output, log):
                    downloaded += 1
                    layouts.append(icao.upper())
                else:
                    unavailable += 1
            except Exception as error:  # noqa: BLE001 - keep going on individual failures
                failed += 1
                if log:
                    log("Failed to download %s layout: %s" % (icao, error_text(error)))
            time.sleep(0.25)
        if progress_cb:
            progress_cb(index, total, icao)
    if log:
        log("Airport layouts complete: %d downloaded, %d overridden, %d unavailable, %d failed."
            % (downloaded, custom, unavailable, failed))
    return {"downloaded": downloaded, "custom": custom, "unavailable": unavailable,
            "failed": failed, "layouts": layouts}
