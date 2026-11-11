#!/usr/bin/env python3
"""Build an AIRAC navdata release end to end and publish it to GitHub.

One command for the whole cycle:

    python make_airac.py                     # build + publish the current cycle
    python make_airac.py --no-publish        # build only
    python make_airac.py --airac 2611        # force a cycle label
    python make_airac.py --regions LF,ED,K   # restrict per-region ZIPs

Pipeline (each step reports progress; the bar shows overall percent, elapsed
time and ETA):

    1. download + extract the NATS UK eAIP for the current cycle
    2. parse it into SkyRadar's CONFIG/nav files
    3. download X-Plane Gateway airport layouts (CONFIG/ad)
    4. load the global base data (openAIP exports + OurAirports)
    5. merge the base under the SkyHigh data, draw openAIP airspace
    6. package the combined EG ZIP (SkyRadar) and the world ZIP (skymaps)
    7. package one ZIP per ICAO region (auto-derived)
    8. publish everything as a GitHub release (tag airac-<cycle>) via `gh`

Requires the GitHub CLI (`gh`) to be installed and authenticated for step 8.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skyhigh_navdata import GENERATOR_NAME, REGION, __version__
from skyhigh_navdata import aip, base, merge, pack, regions
from skyhigh_navdata.build import build_navdata_from_html
from skyhigh_navdata.cycle import normalize

NAVDATA_REPO = "SkyHigh-NOD/SkyHigh-navdata"


# --- Progress display --------------------------------------------------------

def _enable_ansi() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:  # noqa: BLE001 - cosmetics only
        pass


def _clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return "%02d:%02d" % (seconds // 60, seconds % 60)


class Progress:
    """Single-line progress display: bar, percent, detail, elapsed and ETA.

    Steps carry a weight (their rough share of the total run); a background
    thread redraws the line so long blocking calls still tick.
    """

    WIDTH = 26

    def __init__(self):
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._total_weight = 0.0
        self._done_weight = 0.0
        self._step_weight = 0.0
        self._step_name = ""
        self._step_start = self._started
        self._detail = ""
        self._finished = False
        _enable_ansi()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # -- worker API -----------------------------------------------------

    def step(self, name: str, weight: float, detail: str = "") -> None:
        with self._lock:
            if self._step_weight:
                self._done_weight += self._step_weight
            self._step_name = name
            self._step_weight = weight
            self._step_start = time.monotonic()
            self._detail = detail
            self._render()

    def advance(self, fraction: float, detail: str = "") -> None:
        with self._lock:
            self._step_frac = max(0.0, min(1.0, fraction))
            if detail:
                self._detail = detail
            self._render()

    _step_frac = 0.0

    def log(self, message: str) -> None:
        with self._lock:
            line = " " * (self._line_len + 2)
            sys.stdout.write("\r" + line + "\r" + str(message) + "\n")
            sys.stdout.flush()
            self._render()

    def finish(self) -> None:
        with self._lock:
            self._done_weight += self._step_weight
            self._step_weight = 0.0
            self._detail = "done"
            self._finished = True
            self._render()
        sys.stdout.write("\n")
        sys.stdout.flush()

    # -- rendering ------------------------------------------------------

    def _fraction(self) -> float:
        if self._total_weight <= 0:
            return 0.0
        step_part = self._step_weight * self._step_frac
        return min(1.0, (self._done_weight + step_part) / self._total_weight)

    def _render(self) -> None:
        frac = self._fraction()
        elapsed = time.monotonic() - self._started
        eta = elapsed * (1.0 - frac) / frac if frac > 0.005 else 0.0
        filled = int(round(self.WIDTH * frac))
        bar = "#" * filled + "." * (self.WIDTH - filled)
        name = self._step_name if not self._finished else "complete"
        detail = self._detail
        line = "%3.0f%% [%s] %s%s | %s elapsed | %s left" % (
            frac * 100, bar, name,
            (" - " + detail) if detail else "",
            _clock(elapsed), _clock(eta))
        line = line[:160]
        sys.stdout.write("\r" + line + "\x1b[K" if os.name != "nt" else "\r" + line)
        sys.stdout.flush()
        self._line_len = len(line)

    def _loop(self) -> None:
        while not self._finished:
            with self._lock:
                self._render()
            time.sleep(0.4)


# --- Publish -----------------------------------------------------------------

def _gh(*args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("gh %s failed: %s" % (" ".join(args[:3]), result.stderr.strip()))
    return result


def _chunks(items: list, size: int):
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _zip_summary(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    return {
        "name": path.name,
        "region": str(manifest.get("region") or ""),
        "files": len(manifest.get("files") or []),
        "size_mb": path.stat().st_size / (1024 * 1024),
    }


def write_master_notes(out_dir: Path, cycle_display: str, cycle_code: str) -> Path:
    """Release notes covering every published asset (combined + world + regions)."""
    zips = sorted(out_dir.glob("*_%s.zip" % cycle_code))
    summaries = [_zip_summary(z) for z in zips]
    lines = [
        "# AIRAC %s - SkyHigh navdata" % cycle_display,
        "",
        "Assets for SkyRadar, skymaps and per-region installs.",
        "",
        "| Asset | Region | Files | Size |",
        "|---|---|---|---|",
    ]
    for summary in summaries:
        lines.append("| `%s` | %s | %d | %.1f MB |" % (
            summary["name"], summary["region"], summary["files"], summary["size_mb"]))
    lines += [
        "",
        "- `EG_<cycle>.zip` - full EG package for SkyRadar (global nav base merged "
        "under the SkyHigh data, openAIP airspace)",
        "- `ALL_<cycle>.zip` - world package for skymaps",
        "- `<REGION>_<cycle>.zip` - per-region subsets derived automatically",
        "",
        "Sources: NATS UK eAIP, X-Plane Gateway, openAIP daily exports, OurAirports.",
        "",
    ]
    path = out_dir / ("release-notes-%s.md" % cycle_code)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def publish(out_dir: Path, cycle_code: str, cycle_display: str, log) -> None:
    tag = "airac-%s" % cycle_code
    title = "AIRAC %s - EG SkyRadar navdata" % cycle_display
    zips = sorted(out_dir.glob("*_%s.zip" % cycle_code))
    if not zips:
        raise RuntimeError("No release ZIPs found in %s" % out_dir)
    notes = write_master_notes(out_dir, cycle_display, cycle_code)
    exists = _gh("release", "view", tag, "--repo", NAVDATA_REPO).returncode == 0
    if exists:
        log("Release %s exists - uploading %d asset(s) with clobber" % (tag, len(zips)))
        for batch in _chunks([str(z) for z in zips], 20):
            _gh("release", "upload", tag, "--repo", NAVDATA_REPO, "--clobber", *batch)
        _gh("release", "edit", tag, "--repo", NAVDATA_REPO,
            "--title", title, "--notes-file", str(notes))
    else:
        log("Creating release %s with %d asset(s)" % (tag, len(zips)))
        first, rest = [str(z) for z in zips[:20]], [str(z) for z in zips[20:]]
        _gh("release", "create", tag, "--repo", NAVDATA_REPO,
            "--title", title, "--notes-file", str(notes), "--latest", *first)
        for batch in _chunks(rest, 20):
            _gh("release", "upload", tag, "--repo", NAVDATA_REPO, "--clobber", *batch)
    log("Published: https://github.com/%s/releases/tag/%s" % (NAVDATA_REPO, tag))


# --- Pipeline ----------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="make_airac.py",
        description="Build an AIRAC navdata release end to end and publish it.")
    parser.add_argument("--airac", default=None,
                        help="Override the detected AIRAC cycle, e.g. 2610.")
    parser.add_argument("--skip-layouts", action="store_true",
                        help="Skip the X-Plane Gateway airport layouts.")
    parser.add_argument("--no-base", action="store_true",
                        help="Skip the global base data (openAIP + OurAirports).")
    parser.add_argument("--regions", default=None,
                        help="'auto' (default), a comma list of ICAO prefixes, or 'none'.")
    parser.add_argument("--min-region-airports", type=int, default=10)
    parser.add_argument("--no-publish", action="store_true",
                        help="Build only; do not create/upload the GitHub release.")
    parser.add_argument("--keep-staging", action="store_true",
                        help="Keep the intermediate staging tree for debugging.")
    parser.add_argument("--out", default="dist")
    parser.add_argument("--cache-dir", default=".cache")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    log = None  # progress display carries the output
    progress = Progress()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    region = REGION

    try:
        # 1) AIRAC source -----------------------------------------------------
        progress.step("Downloading NATS eAIP", 22, "resolving cycle")
        stage_dir = out_dir / (".staging-%s" % region)
        if stage_dir.exists():
            shutil.rmtree(stage_dir)
        stage_dir.mkdir(parents=True)
        nav_dir = stage_dir / "CONFIG" / "nav"
        ad_dir = stage_dir / "CONFIG" / "ad"
        bg_dir = stage_dir / "CONFIG" / "bg"

        detected_display, html_dir = aip.download_and_extract_airac(
            out_dir / "airac", log=progress.log)
        if args.airac:
            cycle_display, cycle_code = normalize(str(args.airac))
        else:
            cycle_display, cycle_code = normalize(detected_display)
        progress.log("AIRAC cycle: %s (%s)" % (cycle_display, cycle_code))

        # 2) nav files --------------------------------------------------------
        progress.step("Parsing eAIP into CONFIG/nav", 6, "")
        result = build_navdata_from_html(html_dir, nav_dir, region, cycle_display, log=progress.log)
        counts = dict(result["counts"])
        icaos = result["icaos"]

        # 3) airport layouts --------------------------------------------------
        if args.skip_layouts:
            progress.step("Airport layouts (skipped)", 1, "")
            counts["layouts"] = 0
        else:
            progress.step("X-Plane Gateway airport layouts", 14, "0/%d" % len(icaos))
            custom_icaos = pack.custom_ad_icaos(Path(__file__).resolve().parent / "CONFIG")
            summary = aip.download_all_ad_layouts(
                icaos, ad_dir, log=None, skip=custom_icaos,
                progress_cb=lambda done, total, icao:
                    progress.advance(done / max(total, 1),
                                     "%s (%d/%d)" % (icao, done, total)))
            counts.update({k: summary[k] for k in
                           ("downloaded", "custom", "unavailable", "failed")
                           if k in summary})
            counts["layouts"] = summary["downloaded"]

        # 4) global base ------------------------------------------------------
        base_data = None
        if not args.no_base:
            progress.step("openAIP global exports", 20, "listing")
            openaip = base.load_openaip(
                Path(args.cache_dir), log=None,
                progress_cb=lambda done, total, label:
                    progress.advance(done / max(total, 1), label))
            progress.step("OurAirports CSV", 2, "")
            ourairports = base.load_ourairports(Path(args.cache_dir), log=progress.log)
            base_data = dict(openaip)
            base_data["ourairports"] = ourairports
            counts.update(ourairports.get("counts", {}))
            counts.update(openaip.get("counts", {}))

        # 5) merge + airspace --------------------------------------------------
        progress.step("Merging base + openAIP airspace", 5, "")
        if base_data:
            counts.update(merge.merge_base_into_stage(stage_dir, base_data, log=None))
            added, _group = regions.write_openaip_airspace_group(
                bg_dir, base_data.get("airspaces", []),
                regions.EG_REGION["bbox"], cycle_display,
                countries=regions.EG_REGION["countries"])
            counts["airspace_drawings"] = added

        # hand-maintained overrides always win
        overrides_dir = Path(__file__).resolve().parent / "CONFIG"
        if overrides_dir.is_dir():
            applied = pack.apply_overrides(stage_dir, overrides_dir, log=None)
            counts["overrides"] = len(applied)

        (nav_dir / "airac.txt").write_text(
            "region: %s\nairac: %s\nairac_code: %s\nbuilt_utc: %s\ngenerator: %s %s\n" % (
                region, cycle_display, cycle_code,
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                GENERATOR_NAME, __version__),
            encoding="utf-8")

        # 6) combined EG ZIP ---------------------------------------------------
        progress.step("Packaging EG combined ZIP", 5, "")
        manifest = pack.build_manifest(
            stage_dir, region=region, airac=cycle_display, airac_code=cycle_code,
            generator=GENERATOR_NAME, generator_version=__version__, counts=counts)
        pack.write_manifest(stage_dir, manifest)
        eg_zip = pack.make_zip(stage_dir, out_dir / pack.asset_name(region, cycle_code))
        problems = pack.verify_zip(eg_zip, manifest, counts)
        if problems:
            raise RuntimeError("Combined ZIP failed verification: %s" % "; ".join(problems))
        pack.write_release_notes(eg_zip.with_suffix(".md"), manifest)

        # 7) world ZIP ----------------------------------------------------------
        world_zip = None
        if base_data:
            progress.step("Packaging world ZIP (skymaps)", 5, "")
            world_zip = regions.build_global_zip(
                stage_dir, out_dir, cycle_display, cycle_code,
                generator_version=__version__, log=progress.log)

        # 8) region ZIPs --------------------------------------------------------
        region_zips = []
        region_spec = (args.regions or "").strip().lower()
        if region_spec != "none":
            chosen = regions.resolve_regions(args.regions, stage_dir, args.min_region_airports)
            progress.step("Region ZIPs", 8, "0/%d" % len(chosen))
            for index, prefix in enumerate(chosen, start=1):
                progress.advance(index / max(len(chosen), 1), prefix)
                region_zips.append(regions.build_region_zip(
                    prefix, stage_dir, out_dir, cycle_display, cycle_code,
                    base_airspaces=(base_data or {}).get("airspaces"),
                    generator_version=__version__))

        if not args.keep_staging:
            shutil.rmtree(stage_dir, ignore_errors=True)

        # 9) publish ------------------------------------------------------------
        if not args.no_publish:
            progress.step("Publishing to GitHub release", 4, "checking release")
            publish(out_dir, cycle_code, cycle_display, progress.log)
        else:
            progress.step("Build complete (not published)", 1, "")

        progress.finish()
        print("Done: AIRAC %s (%s) in %s" % (cycle_display, cycle_code, out_dir))
        return 0
    except KeyboardInterrupt:
        progress.finish()
        return 130
    except Exception as error:  # noqa: BLE001 - surface after closing the bar
        progress.finish()
        print("FAILED: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
