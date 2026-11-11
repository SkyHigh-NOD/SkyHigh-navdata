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
from skyhigh_navdata import build as build_navdata
from skyhigh_navdata.build import build_navdata_from_html
from skyhigh_navdata.cycle import normalize

NAVDATA_REPO = "SkyHigh-NOD/SkyHigh-navdata"

# Rough share of the total run each step takes — drives the overall percent
# and ETA. Skipped steps are stepped through and marked done instantly.
STEP_PLAN = [
    ("Downloading NATS eAIP", 22),
    ("Parsing eAIP into CONFIG/nav", 6),
    ("X-Plane Gateway airport layouts", 14),
    ("openAIP global exports", 20),
    ("OurAirports CSV", 2),
    ("Merging base + openAIP airspace", 5),
    ("Packaging the release ZIP", 5),
    ("Packaging world ZIP (skymaps)", 5),
    ("Region ZIPs", 8),
    ("Publishing to GitHub release", 4),
    ("Done", 1),
]

# Selective regeneration (--only): the individual pieces of CONFIG/nav plus
# the layout, airspace and eAIP-download steps.
def _zip_cycle(zip_path: Path) -> tuple[str, str]:
    """(airac_code, airac_display) from a built navdata ZIP's manifest."""
    with zipfile.ZipFile(zip_path) as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    return (str(manifest.get("airac_code") or ""),
            str(manifest.get("airac") or ""))


def run_selective(args, progress, out_dir: Path, only: set) -> int:
    """Regenerate part of an existing navdata in place.

    Seeds the staging tree from an existing ZIP (--from-zip or the newest in
    out_dir), runs just the requested generators against the cached eAIP HTML,
    re-merges the global base (regeneration wipes merged entries), re-packages
    under the seed ZIP's name and publishes.
    """
    region = REGION
    stage_dir = out_dir / (".staging-%s" % region)

    # 1) seed staging + cycle ---------------------------------------------------
    progress.step("Downloading NATS eAIP", "seeding staging")
    seed = args.from_zip
    if not seed:
        candidates = [p for p in out_dir.glob("*.zip")
                      if p.stat().st_size > 100_000]
        if candidates:
            seed = str(max(candidates, key=lambda p: p.stat().st_mtime))
    if not (stage_dir / "CONFIG").exists():
        if not seed or not Path(seed).is_file():
            progress.finish("FAILED")
            print("No staging tree and no seed ZIP found. Run the full build once, "
                  "or pass --from-zip <path>.", file=sys.stderr)
            return 1
        progress.log("Seeding staging from %s" % seed)
        with zipfile.ZipFile(seed) as archive:
            archive.extractall(stage_dir)
    manifest_path = stage_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            seed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            cycle_code = str(seed_manifest.get("airac_code") or "")
            cycle_display = str(seed_manifest.get("airac") or "")
        except ValueError:
            cycle_code, cycle_display = "", ""
    else:
        cycle_code, cycle_display = "", ""
    if not cycle_code:
        progress.finish("FAILED")
        print("The seed navdata has no manifest.json.", file=sys.stderr)
        return 1
    progress.log("AIRAC cycle: %s (%s) - regenerating: %s"
                 % (cycle_display, cycle_code, ", ".join(sorted(only))))
    progress.advance(1.0, "staging seeded")

    html_dir = out_dir / "airac" / "html"
    nav_wanted = sorted(only & set(NAV_ITEMS))

    # 2) eAIP HTML availability -------------------------------------------------
    if "download" in only:
        progress.advance(0.1, "re-downloading NATS eAIP")
        zip_path = out_dir / "airac" / "nats-airac.zip"
        if zip_path.exists():
            zip_path.unlink()
        _detected, html_dir2 = aip.download_and_extract_airac(
            out_dir / "airac", log=progress.log)
        html_dir = html_dir2
        progress.advance(1.0, "eAIP refreshed")
    elif nav_wanted and not html_dir.is_dir():
        progress.finish("FAILED")
        print("eAIP HTML is not cached (dist/airac/html). Add 'download' to --only.",
              file=sys.stderr)
        return 1

    # 3) regenerate the requested nav files --------------------------------------
    progress.step("Parsing eAIP into CONFIG/nav",
                  "regenerating: %s" % (", ".join(nav_wanted) or "none"))
    nav_dir = stage_dir / "CONFIG" / "nav"
    icaos = []
    if nav_wanted:
        builder_map = {
            "fixes": lambda: build_navdata.build_fixes_from_html(
                html_dir, nav_dir, cycle_display, log=None),
            "navaids": lambda: build_navdata.build_navaids_from_html(
                html_dir, nav_dir, region, cycle_display, log=None),
            "airways": lambda: build_navdata.build_airways_from_html(
                html_dir, nav_dir, region, cycle_display, log=None),
            "ad-positions": lambda: build_navdata.build_ad_positions_from_html(
                html_dir, nav_dir, region, cycle_display, log=None),
            "entry-exit": lambda: build_navdata.build_ad_entry_exit_from_html(
                html_dir, nav_dir, region, cycle_display, log=None),
            "ctr": lambda: build_navdata.build_ctr_positions(
                nav_dir, cycle_display, log=None),
        }
        for index, name in enumerate(nav_wanted, start=1):
            progress.advance((index - 1) / len(nav_wanted), name)
            result = builder_map[name]()
            progress.log("%s: regenerated" % name)
        progress.advance(1.0)
    else:
        progress.advance(1.0, "kept")

    # 4) layouts ------------------------------------------------------------------
    progress.step("X-Plane Gateway airport layouts", "")
    if "layouts" in only:
        if not icaos:
            positions_path = nav_dir / "AD-positions"
            if positions_path.is_file():
                icaos = [line.split()[0].upper()
                         for line in positions_path.read_text(
                             encoding="utf-8", errors="replace").splitlines()
                         if line.strip() and line.split()[0][:1].isalpha()]
        ad_dir = stage_dir / "CONFIG" / "ad"
        custom_icaos = pack.custom_ad_icaos(Path(__file__).resolve().parent / "CONFIG")
        summary = aip.download_all_ad_layouts(
            icaos, ad_dir, log=None, skip=custom_icaos,
            progress_cb=lambda done, total, icao:
                progress.advance(done / max(total, 1),
                                 "%s (%d/%d)" % (icao, done, total)))
        progress.log("layouts: %d downloaded, %d failed"
                     % (summary["downloaded"], summary["failed"]))
        progress.advance(1.0)
    else:
        progress.advance(1.0, "kept")

    # 5) base data + merge ---------------------------------------------------------
    base_data = None
    need_nav_merge = bool(nav_wanted)
    if "airspace" in only or need_nav_merge:
        progress.step("openAIP global exports", "listing")
        openaip = base.load_openaip(
            Path(args.cache_dir), log=None,
            progress_cb=lambda done, total, label:
                progress.advance(done / max(total, 1), label))
        base_data = dict(openaip)
        if need_nav_merge:
            progress.step("OurAirports CSV", "")
            ourairports = base.load_ourairports(Path(args.cache_dir), log=progress.log)
            base_data["ourairports"] = ourairports

    progress.step("Merging base + openAIP airspace", "")
    if need_nav_merge and base_data:
        counts_update = merge.merge_base_into_stage(stage_dir, base_data, log=None)
        progress.log("base merge: %s"
                     % ", ".join("%s=%d" % item for item in counts_update.items()))
    else:
        progress.advance(0.5, "nav files kept")
    if "airspace" in only:
        if base_data:
            bg_dir = stage_dir / "CONFIG" / "bg"
            oaip_dir = bg_dir / "OAIP"
            if oaip_dir.is_dir():
                shutil.rmtree(oaip_dir)
            added, _group = regions.write_openaip_airspace_group(
                bg_dir, base_data.get("airspaces", []),
                regions.EG_REGION["bbox"], cycle_display,
                countries=regions.EG_REGION["countries"])
            progress.log("airspace: %d openAIP drawing(s)" % added)
        progress.advance(1.0)
    else:
        progress.advance(1.0, "kept")

    # overrides always win
    overrides_dir = Path(__file__).resolve().parent / "CONFIG"
    if overrides_dir.is_dir():
        pack.apply_overrides(stage_dir, overrides_dir, log=None)
    (nav_dir / "airac.txt").write_text(
        "region: %s\nairac: %s\nairac_code: %s\nbuilt_utc: %s\ngenerator: %s %s\n"
        "regenerated: %s\n" % (
            region, cycle_display, cycle_code,
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            GENERATOR_NAME, __version__, ", ".join(sorted(only))),
        encoding="utf-8")

    # 6) package -------------------------------------------------------------------
    progress.step("Packaging the release ZIP", "")
    counts = {}
    if manifest_path.is_file():
        try:
            counts = dict(json.loads(manifest_path.read_text(encoding="utf-8"))
                          .get("counts") or {})
        except ValueError:
            counts = {}
    manifest = pack.build_manifest(
        stage_dir, region=region, airac=cycle_display, airac_code=cycle_code,
        generator=GENERATOR_NAME, generator_version=__version__, counts=counts)
    pack.write_manifest(stage_dir, manifest)
    # the seed ZIP's stem decides the output name (2611.zip stays 2611.zip)
    seed_stem = Path(seed).stem if seed and Path(seed).is_file() \
        else pack.asset_name(region, cycle_code)
    zip_path = pack.make_zip(stage_dir, out_dir / ("%s.zip" % seed_stem))
    problems = pack.verify_zip(zip_path, manifest)
    if problems:
        raise RuntimeError("Repackaged ZIP failed verification: %s" % "; ".join(problems))
    progress.log("packaged %s" % zip_path.name)

    progress.step("Packaging world ZIP (skymaps)", "kept (--only)")
    progress.advance(1.0)
    progress.step("Region ZIPs", "kept (--only)")
    progress.advance(1.0)

    if not args.keep_staging:
        shutil.rmtree(stage_dir, ignore_errors=True)

    # 7) publish ---------------------------------------------------------------------
    if args.no_publish:
        progress.step("Publishing to GitHub release", "skipped (--no-publish)")
        progress.advance(1.0)
    else:
        progress.step("Publishing to GitHub release", "checking release")
        publish([zip_path], cycle_code, cycle_display, progress.log)

    progress.step("Done", "")
    progress.finish("done")
    print("Done: regenerated %s for AIRAC %s (%s)"
          % (", ".join(sorted(only)), cycle_display, cycle_code))
    return 0
# Selective regeneration (--only): the individual pieces of CONFIG/nav plus
# the layout, airspace and eAIP-download steps.
NAV_ITEMS = ("fixes", "navaids", "airways", "ad-positions", "entry-exit", "ctr")
ONLY_ITEMS = set(NAV_ITEMS) | {"download", "layouts", "airspace"}


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

    The whole run is planned up front (``plan`` takes (name, weight) pairs);
    each ``step(name)`` must be declared there, so overall percent and ETA are
    accurate. A background thread redraws the line so long blocking calls
    still tick.
    """

    WIDTH = 26

    def __init__(self):
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._weights: dict[str, float] = {}
        self._total_weight = 0.0
        self._done_weight = 0.0
        self._step_weight = 0.0
        self._step_frac = 0.0
        self._step_name = ""
        self._detail = ""
        self._finished = False
        self._line_len = 0
        _enable_ansi()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # -- worker API -----------------------------------------------------

    def plan(self, steps: list) -> None:
        with self._lock:
            self._weights = {name: float(weight) for name, weight in steps}
            self._total_weight = sum(weight for _, weight in steps)

    def step(self, name: str, detail: str = "") -> None:
        """Begin a planned step; skipped steps just advance(1.0) immediately."""
        with self._lock:
            if name not in self._weights:
                raise KeyError("unplanned step: %r" % name)
            self._done_weight += self._step_weight
            self._step_weight = self._weights[name]
            self._step_frac = 0.0
            self._step_name = name
            self._detail = detail
            self._render()

    def advance(self, fraction: float, detail: str = "") -> None:
        with self._lock:
            self._step_frac = max(0.0, min(1.0, fraction))
            if detail:
                self._detail = detail
            self._render()

    def log(self, message: str) -> None:
        with self._lock:
            line = " " * (self._line_len + 2)
            sys.stdout.write("\r" + line + "\r" + str(message) + "\n")
            sys.stdout.flush()
            self._render()

    def finish(self, label: str = "done") -> None:
        with self._lock:
            self._done_weight += self._step_weight
            self._step_weight = 0.0
            self._step_frac = 0.0
            self._detail = label
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
        # never exceed the console width — a wrapped line defeats the \r
        width = max(40, shutil.get_terminal_size((100, 24)).columns - 2)
        line = line[:width].ljust(width)
        sys.stdout.write("\r" + line)
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


def write_master_notes(out_dir: Path, cycle_display: str, cycle_code: str,
                       zips: list) -> Path:
    """Release notes covering every published asset."""
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
        "- `<cycle>.zip` - everything in one package: the global nav base merged "
        "under the SkyHigh data, openAIP airspace",
        "- `ALL_<cycle>.zip` - world package for skymaps",
        "- `<REGION>_<cycle>.zip` - per-region subsets derived automatically",
        "",
        "Sources: NATS UK eAIP, X-Plane Gateway, openAIP daily exports, OurAirports.",
        "",
    ]
    path = out_dir / ("release-notes-%s.md" % cycle_code)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def publish(zips: list, cycle_code: str, cycle_display: str, log) -> None:
    tag = "airac-%s" % cycle_code
    title = "AIRAC %s - SkyHigh navdata" % cycle_display
    if not zips:
        raise RuntimeError("No release ZIPs found")
    zips = [Path(z) for z in zips]
    notes = write_master_notes(zips[0].parent, cycle_display, cycle_code, zips)
    # "release not found" is the expected signal to create it — only route
    # real gh failures through _gh()
    view = subprocess.run(["gh", "release", "view", tag, "--repo", NAVDATA_REPO],
                          capture_output=True, text=True)
    exists = view.returncode == 0
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
    parser.add_argument("--single", action="store_true",
                        help="Publish ONE asset per cycle: <cycle>.zip (e.g. 2611.zip) "
                             "containing everything - the global base merged under the "
                             "SkyHigh data. No world/region ZIPs.")
    parser.add_argument("--only", default=None,
                        help="Regenerate only selected parts of an existing navdata, "
                             "comma-separated from: download, fixes, navaids, airways, "
                             "ad-positions, entry-exit, ctr, layouts, airspace. "
                             "Everything else is kept from the seed ZIP (--from-zip, "
                             "default: the newest ZIP in --out). The base merge always "
                             "re-runs after any nav regeneration.")
    parser.add_argument("--from-zip", default=None,
                        help="Seed the staging tree from this existing navdata ZIP "
                             "(used with --only). Default: newest ZIP in --out.")
    parser.add_argument("--keep-staging", action="store_true",
                        help="Keep the intermediate staging tree for debugging.")
    parser.add_argument("--out", default="dist")
    parser.add_argument("--cache-dir", default=".cache")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    progress = Progress()
    progress.plan(STEP_PLAN)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    region = REGION

    try:
        only = None
        if args.only:
            only = {s.strip().lower() for s in args.only.split(",") if s.strip()}
            unknown = only - ONLY_ITEMS
            if unknown:
                raise SystemExit("Unknown --only item(s): %s (known: %s)"
                                 % (", ".join(sorted(unknown)),
                                    ", ".join(sorted(ONLY_ITEMS))))
            return run_selective(args, progress, out_dir, only)

        # 1) AIRAC source -----------------------------------------------------
        progress.step("Downloading NATS eAIP", "waiting for NATS...")
        def _eaip_progress(done: int, total: int) -> None:
            if total:
                progress.advance(done / total, "%.0f/%.0f MB (%d%%)" % (
                    done / 1e6, total / 1e6, done * 100 // total))
            else:
                progress.advance(0.5, "%.1f MB downloaded" % (done / 1e6))

        stage_dir = out_dir / (".staging-%s" % region)
        if stage_dir.exists():
            shutil.rmtree(stage_dir)
        stage_dir.mkdir(parents=True)
        nav_dir = stage_dir / "CONFIG" / "nav"
        ad_dir = stage_dir / "CONFIG" / "ad"
        bg_dir = stage_dir / "CONFIG" / "bg"

        detected_display, html_dir = aip.download_and_extract_airac(
            out_dir / "airac", log=progress.log, progress_cb=_eaip_progress)
        progress.log("Extracted AIRAC archive")
        # normalize() returns (code, display) — mind the order
        if args.airac:
            cycle_code, cycle_display = normalize(str(args.airac))
        else:
            cycle_code, cycle_display = normalize(detected_display)
        progress.log("AIRAC cycle: %s (%s)" % (cycle_display, cycle_code))

        # 2) nav files --------------------------------------------------------
        progress.step("Parsing eAIP into CONFIG/nav", "")
        result = build_navdata_from_html(html_dir, nav_dir, region, cycle_display, log=None)
        counts = dict(result["counts"])
        icaos = result["icaos"]

        # 3) airport layouts --------------------------------------------------
        progress.step("X-Plane Gateway airport layouts", "0/%d" % len(icaos))
        if args.skip_layouts:
            progress.advance(1.0, "skipped")
            counts["layouts"] = 0
        else:
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
            progress.step("openAIP global exports", "listing")
            openaip = base.load_openaip(
                Path(args.cache_dir), log=None,
                progress_cb=lambda done, total, label:
                    progress.advance(done / max(total, 1), label))
            progress.step("OurAirports CSV", "")
            ourairports = base.load_ourairports(Path(args.cache_dir), log=progress.log)
            base_data = dict(openaip)
            base_data["ourairports"] = ourairports
            counts.update(ourairports.get("counts", {}))
            counts.update(openaip.get("counts", {}))

        # 5) merge + airspace --------------------------------------------------
        progress.step("Merging base + openAIP airspace", "")
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

        # 6) the release ZIP/ZIPs --------------------------------------------
        # Single mode: one <cycle>.zip with everything in it (the manifest
        # keeps region EG so a future SkyRadar retarget still installs it).
        single = args.single
        progress.step("Packaging the release ZIP", "")
        manifest = pack.build_manifest(
            stage_dir, region=region, airac=cycle_display, airac_code=cycle_code,
            generator=GENERATOR_NAME, generator_version=__version__, counts=counts)
        pack.write_manifest(stage_dir, manifest)
        asset_name = ("%s.zip" % cycle_code) if single else pack.asset_name(region, cycle_code)
        eg_zip = pack.make_zip(stage_dir, out_dir / asset_name)
        problems = pack.verify_zip(eg_zip, manifest, counts)
        if problems:
            raise RuntimeError("Combined ZIP failed verification: %s" % "; ".join(problems))
        if not single:
            pack.write_release_notes(eg_zip.with_suffix(".md"), manifest)

        # 7) world ZIP ----------------------------------------------------------
        world_zip = None
        if single:
            progress.step("Packaging world ZIP (skymaps)", "skipped (--single)")
            progress.advance(1.0)
        elif base_data:
            progress.step("Packaging world ZIP (skymaps)", "")
            world_zip = regions.build_global_zip(
                stage_dir, out_dir, cycle_display, cycle_code,
                generator_version=__version__, log=progress.log)

        # 8) region ZIPs --------------------------------------------------------
        region_zips = []
        region_spec = (args.regions or "").strip().lower()
        if single:
            progress.step("Region ZIPs", "skipped (--single)")
            progress.advance(1.0)
        elif region_spec != "none":
            chosen = regions.resolve_regions(args.regions, stage_dir, args.min_region_airports)
            progress.step("Region ZIPs", "0/%d" % len(chosen))
            for index, prefix in enumerate(chosen, start=1):
                progress.advance(index / max(len(chosen), 1), prefix)
                region_zips.append(regions.build_region_zip(
                    prefix, stage_dir, out_dir, cycle_display, cycle_code,
                    base_airspaces=(base_data or {}).get("airspaces"),
                    generator_version=__version__))

        if not args.keep_staging:
            shutil.rmtree(stage_dir, ignore_errors=True)

        # 9) publish ------------------------------------------------------------
        release_zips = [eg_zip]
        if world_zip:
            release_zips.append(world_zip)
        release_zips.extend(z for z in region_zips if z)
        if args.no_publish:
            progress.step("Publishing to GitHub release", "skipped (--no-publish)")
            progress.advance(1.0)
        else:
            progress.step("Publishing to GitHub release", "checking release")
            publish(release_zips, cycle_code, cycle_display, progress.log)

        progress.step("Done", "")
        progress.finish("done")
        print("Done: AIRAC %s (%s) in %s" % (cycle_display, cycle_code, out_dir))
        return 0
    except KeyboardInterrupt:
        progress.finish("STOPPED")
        print("Stopped by user.", file=sys.stderr)
        return 130
    except Exception as error:  # noqa: BLE001 - surface after closing the bar
        progress.finish("FAILED")
        print("FAILED: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
