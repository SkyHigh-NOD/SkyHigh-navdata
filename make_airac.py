#!/usr/bin/env python3
"""Build an AIRAC navdata release end to end. Publishing is a separate, explicit step.

One command for the whole cycle:

    python make_airac.py                     # build the current cycle into dist/
    python make_airac.py --publish           # build, then upload to the GitHub release
    python make_airac.py --publish --clobber # ... replacing assets already on the release
    python make_airac.py --airac 2611        # force a cycle label
    python make_airac.py --regions LF,ED,K   # restrict per-region ZIPs

Nothing leaves the machine without --publish. SkyRadar installs pull from the
release, so an upload replaces what every install gets; assets already on a
release are only replaced with --clobber.

Pipeline (each step reports progress; the bar shows overall percent, elapsed
time and ETA):

    1. download + extract the NATS UK eAIP for the current cycle
    2. parse it into SkyRadar's CONFIG/nav files
    3. download X-Plane Gateway airport layouts (CONFIG/ad)
    4. load the global base data (openAIP exports + OurAirports)
    5. draw the airspace (eAIP drawings and openAIP per-country groups),
       apply the CONFIG/ overrides, merge the base under
    6. package the combined EG ZIP (SkyRadar) and the world ZIP (skymaps)
    7. package one ZIP per ICAO region (auto-derived)
    8. with --publish: upload everything to the GitHub release (tag
       airac-<cycle>) via `gh`

Step 8 requires the GitHub CLI (`gh`) to be installed and authenticated.
"""
from __future__ import annotations

import argparse
import json
import os
import re
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
from skyhigh_navdata.airspace import build_airspace_from_html
from skyhigh_navdata.build import build_navdata_from_html
from skyhigh_navdata.cycle import normalize

NAVDATA_REPO = "SkyHigh-NOD/SkyHigh-navdata"

# Rough share of the total run each step takes — drives the overall percent
# and ETA. A step that does not run is passed to Progress.skip(), which counts
# its weight as done so the bar still ends at 100%.
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

# The nav files the base merge appends to. An override of one of these has to
# be in place before the merge, or the merge's rows are lost with the copy.
MERGE_TARGETS = {"CONFIG/nav/navaid.dat", "CONFIG/nav/AD-positions"}

OVERRIDES_DIR = Path(__file__).resolve().parent / "CONFIG"


def _ad_positions_icaos(positions_path: Path) -> list:
    """All aerodrome ICAOs from a CONFIG/nav/AD-positions file."""
    return [line.split()[0].upper()
            for line in positions_path.read_text(
                encoding="utf-8", errors="replace").splitlines()
            if line.strip() and line.split()[0][:1].isalpha()]


def _parse_layout_regions(spec: str) -> list:
    """Validate a comma-separated ICAO region prefix list (e.g. 'EG,LF,K')."""
    prefixes = [part.strip().upper() for part in (spec or "").split(",")]
    prefixes = [prefix for prefix in prefixes if prefix]
    if not prefixes:
        raise ValueError("--layouts region list is empty.")
    for prefix in prefixes:
        if not re.match(r"^[A-Z]{1,2}$", prefix):
            raise ValueError("Bad ICAO region prefix in --layouts: %r" % prefix)
    return prefixes


def _layout_icaos(spec: str, positions_path: Path, eaip_icaos: list) -> list:
    """Aerodrome ICAO list for a layout spec: 'eg' (the eAIP aerodromes),
    'world' (every aerodrome in the merged base) or a comma-separated list of
    ICAO region prefixes to add to / refresh in an existing set (e.g.
    'EG,LF' or 'ED')."""
    spec = (spec or "eg").strip().lower()
    if spec == "eg":
        pool = list(eaip_icaos or [])
        if not pool:
            pool = _ad_positions_icaos(positions_path)
        return [icao for icao in pool if regions.region_prefix(icao) == "EG"]
    if spec == "world":
        return _ad_positions_icaos(positions_path)
    prefixes = set(_parse_layout_regions(spec))
    return [icao for icao in _ad_positions_icaos(positions_path)
            if regions.region_prefix(icao) in prefixes]


def _zip_cycle(zip_path: Path) -> tuple[str, str]:
    """(airac_code, airac_display) from a built navdata ZIP's manifest."""
    with zipfile.ZipFile(zip_path) as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    return (str(manifest.get("airac_code") or ""),
            str(manifest.get("airac") or ""))


def combined_seed(out_dir: Path):
    """The newest *combined* ZIP in ``out_dir``, or None.

    Only ``EG_<cycle>.zip`` and the single ``<cycle>.zip`` are the whole
    navdata. A world or region ZIP is a subset under another name: seeding
    from one and repackaging it would overwrite that asset with the wrong
    contents.
    """
    pattern = re.compile(r"^(?:%s_)?\d{4}\.zip$" % re.escape(REGION), re.I)
    candidates = [path for path in Path(out_dir).glob("*.zip") if pattern.match(path.name)]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


AIRSPACE_SOURCES = {
    "eaip": "NATS UK eAIP ENR 2.1 / AD 2.17",
    "openaip": "openAIP daily exports (CC BY-NC 4.0)",
    "both": "NATS UK eAIP ENR 2.1 / AD 2.17, and openAIP daily exports (CC BY-NC 4.0)",
}


def build_sources(base_data, layouts: str, overrides: int, previous=None,
                  airspace: str = "") -> dict:
    """The manifest's ``sources``: where each part of the package came from."""
    sources = dict(previous or {})
    sources["aip"] = "NATS UK eAIP (https://nats-uk.ead-it.com)"
    if base_data:
        sources.update(base.SOURCES)
    if airspace:
        sources["airspace"] = AIRSPACE_SOURCES[airspace]
    if layouts:
        sources["layouts"] = layouts
    sources["overrides"] = ("%d file(s) from the repository's CONFIG folder" % overrides
                            if overrides else "none")
    return sources


def sealed_manifest(stage_dir: Path, **fields) -> dict:
    """Build and write the manifest, refusing one that names a local path."""
    manifest = pack.build_manifest(stage_dir, **fields)
    leaked = pack.local_paths_in(manifest)
    if leaked:
        raise RuntimeError(
            "The manifest would publish a path from this machine (%s). "
            "Describe the source in words instead." % ", ".join(leaked))
    pack.write_manifest(stage_dir, manifest)
    return manifest


def airspace_plan(source: str, has_base: bool) -> tuple:
    """(draw the eAIP airspace, draw the openAIP airspace) for a source choice.

    ``both`` without base data falls back to the eAIP drawings alone.
    """
    return source in ("both", "eaip"), source in ("both", "openaip") and has_base


def run_selective(args, progress, out_dir: Path, only: set) -> int:
    """Regenerate part of an existing navdata in place.

    Seeds the staging tree from an existing combined ZIP (--from-zip or the
    newest combined ZIP in out_dir), runs just the requested generators
    against the cached eAIP HTML, applies the overrides, re-merges the global
    base and re-packages under the seed ZIP's name. Uploads only with
    --publish.
    """
    region = REGION
    stage_dir = out_dir / (".staging-%s" % region)

    # 1) seed staging + cycle ---------------------------------------------------
    progress.step("Downloading NATS eAIP", "seeding staging")
    seed = Path(args.from_zip) if args.from_zip else combined_seed(out_dir)
    manifest_path = stage_dir / "manifest.json"
    if not (stage_dir / "CONFIG").exists() or not manifest_path.is_file():
        if not seed or not seed.is_file():
            progress.finish("FAILED")
            print("No staging tree and no combined ZIP (%s_<cycle>.zip or <cycle>.zip) "
                  "found. Run the full build once, or pass --from-zip <path>."
                  % region, file=sys.stderr)
            return 1
        if stage_dir.exists():
            # a leftover staging tree without a manifest is unusable — the
            # release ZIP is the source of truth, so start over from it
            progress.log("Staging tree is incomplete - re-seeding from %s" % seed.name)
            shutil.rmtree(stage_dir, ignore_errors=True)
        progress.log("Seeding staging from %s" % seed.name)
        with zipfile.ZipFile(seed) as archive:
            pack.safe_extract(archive, stage_dir)
    seed_manifest = {}
    if manifest_path.is_file():
        try:
            seed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            seed_manifest = {}
    cycle_code = str(seed_manifest.get("airac_code") or "")
    cycle_display = str(seed_manifest.get("airac") or "")
    if not cycle_code:
        progress.finish("FAILED")
        print("The seed navdata has no usable manifest.json.", file=sys.stderr)
        return 1
    if str(seed_manifest.get("region") or region).upper() != region:
        progress.finish("FAILED")
        print("The seed is a %s package, not the combined %s navdata."
              % (seed_manifest.get("region"), region), file=sys.stderr)
        return 1
    progress.log("AIRAC cycle: %s (%s) - regenerating: %s"
                 % (cycle_display, cycle_code, ", ".join(sorted(only))))
    progress.advance(1.0, "staging seeded")

    html_dir = out_dir / "airac" / "html"
    nav_wanted = sorted(only & set(NAV_ITEMS))

    override_paths = [relative for relative, _source in pack.iter_overrides(OVERRIDES_DIR)]
    need_nav_merge = bool(nav_wanted) or bool(MERGE_TARGETS & set(override_paths))

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
    elif (nav_wanted or ("airspace" in only and args.airspace_source != "openaip")) \
            and not html_dir.is_dir():  # eAIP drawings are parsed from the HTML too
        progress.finish("FAILED")
        print("eAIP HTML is not cached (dist/airac/html). Add 'download' to --only.",
              file=sys.stderr)
        return 1

    # From here on the staged files change. The staged manifest describes
    # them as they were, so it goes first: if this run fails part way, the
    # next one finds a tree without a manifest and seeds again from the ZIP
    # instead of working on half-changed files. The new manifest is written
    # only once everything below has succeeded.
    if manifest_path.exists():
        manifest_path.unlink()

    # The seed already holds the base rows of the build that made it. When
    # this run merges again they come out first, so the merge starts from the
    # SkyHigh rows and openAIP still beats OurAirports for every aerodrome.
    if need_nav_merge:
        merge.strip_base_rows(stage_dir, seed_manifest, region=region, log=progress.log)

    # 3) regenerate the requested nav files --------------------------------------
    progress.step("Parsing eAIP into CONFIG/nav",
                  "regenerating: %s" % (", ".join(nav_wanted) or "none"))
    nav_dir = stage_dir / "CONFIG" / "nav"
    bg_dir = stage_dir / "CONFIG" / "bg"
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
            builder_map[name]()
            progress.log("%s: regenerated" % name)
        progress.advance(1.0)
    else:
        progress.advance(1.0, "kept")

    # 4) base data -----------------------------------------------------------------
    # The overrides are copied in before the merge (step 6), as in
    # build_navdata.py. An override of a merged nav file replaces the seed's
    # copy, base rows included, so the merge has to run again after it.
    base_data = None
    openaip_airspace = "airspace" in only and args.airspace_source != "eaip"
    if openaip_airspace or "layouts" in only or need_nav_merge:
        progress.step("openAIP global exports", "listing")
        openaip = base.load_openaip(
            Path(args.cache_dir), log=None,
            progress_cb=lambda done, total, label:
                progress.advance(done / max(total, 1), label))
        progress.step("OurAirports CSV", "")
        base_data = base.require_base(
            openaip, base.load_ourairports(Path(args.cache_dir), log=progress.log))
    else:
        progress.skip("openAIP global exports", "not needed")
        progress.skip("OurAirports CSV", "not needed")

    # 5) airspace, 6) overrides + merge ---------------------------------------------
    progress.step("Merging base + openAIP airspace", "")
    airspace_added = None
    eaip_airspace_counts = {}
    if "airspace" in only and args.airspace_source != "openaip":
        eaip_airspace_counts = build_airspace_from_html(
            html_dir, bg_dir, region, cycle_display, log=None)
        progress.log("airspace: eAIP drawings regenerated")
    if openaip_airspace:
        if not base_data or not base_data.get("airspaces"):
            raise RuntimeError("openAIP returned no airspace; the existing drawings were kept.")
        airspace_added = regions.write_openaip_airspace(
            bg_dir, base_data, args.airspace_scope, cycle_display, progress.log)
    applied = pack.apply_overrides(stage_dir, OVERRIDES_DIR, log=None) \
        if OVERRIDES_DIR.is_dir() else []
    if need_nav_merge and base_data:
        counts_update = merge.merge_base_into_stage(stage_dir, base_data, log=None)
        progress.log("base merge: %s"
                     % ", ".join("%s=%d" % item for item in counts_update.items()))
    else:
        counts_update = {}
    progress.advance(1.0)

    # 7) airport layouts — after the merge, so a region list resolves against
    # the merged worldwide aerodrome list (this is how a region is *added* to
    # an already-built set: its layouts download on top of the seeded ones).
    # Aerodromes with a CONFIG/ad override are skipped, so the override stays.
    if "layouts" in only:
        progress.step("X-Plane Gateway airport layouts", "")
        layout_spec = getattr(args, "layouts", "eg")
        positions_path = nav_dir / "AD-positions"
        icaos = _layout_icaos(layout_spec, positions_path, icaos)
        progress.log("layouts %s: %d aerodrome(s) requested" % (layout_spec, len(icaos)))
        ad_dir = stage_dir / "CONFIG" / "ad"
        custom_icaos = pack.custom_ad_icaos(OVERRIDES_DIR)
        summary = aip.download_all_ad_layouts(
            icaos, ad_dir, log=None, skip=custom_icaos,
            progress_cb=lambda done, total, icao:
                progress.advance(done / max(total, 1), "%s (%d/%d)" % (icao, done, total)))
        progress.log("layouts: %d downloaded, %d failed"
                     % (summary["downloaded"], summary["failed"]))
        progress.advance(1.0)
    else:
        progress.skip("X-Plane Gateway airport layouts", "kept")

    (nav_dir / "airac.txt").write_text(
        "region: %s\nairac: %s\nairac_code: %s\nbuilt_utc: %s\ngenerator: %s %s\n"
        "regenerated: %s\n" % (
            region, cycle_display, cycle_code,
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            GENERATOR_NAME, __version__, ", ".join(sorted(only))),
        encoding="utf-8")

    # 8) package -------------------------------------------------------------------
    progress.step("Packaging the release ZIP", "")
    counts = dict(seed_manifest.get("counts") or {})
    counts.update(counts_update)
    counts.update(eaip_airspace_counts)
    counts["overrides"] = len(applied)
    if airspace_added is not None:
        counts["airspace_drawings"] = airspace_added
    if "layouts" in only:
        # the set may have grown (a region was added); count the actual
        # layout files so the manifest matches the ZIP
        ad_count = sum(1 for path in (stage_dir / "CONFIG" / "ad").glob("*.dat"))
        counts["layouts"] = max(0, ad_count - (counts.get("layouts_custom") or 0))
    previous_sources = seed_manifest.get("sources")
    sources = build_sources(
        base_data, "", len(applied),
        previous=previous_sources if isinstance(previous_sources, dict) else None,
        airspace=args.airspace_source if "airspace" in only else "")
    if "airspace" in only:
        # what the tree holds now: eAIP drawings kept from the seed are still
        # credited when only the openAIP groups were redrawn, and the reverse
        sources["airspace"] = regions.airspace_label(bg_dir)
    sources["regenerated"] = ", ".join(sorted(only))
    manifest = sealed_manifest(
        stage_dir, region=region, airac=cycle_display, airac_code=cycle_code,
        sources=sources, counts=counts, overrides=applied,
        generator=GENERATOR_NAME, generator_version=__version__)
    # the seed ZIP's stem decides the output name (2611.zip stays 2611.zip)
    seed_stem = seed.stem if seed and seed.is_file() \
        else Path(pack.asset_name(region, cycle_code)).stem
    zip_path = pack.make_zip(stage_dir, out_dir / ("%s.zip" % seed_stem))
    problems = pack.verify_zip(zip_path, manifest)
    if problems:
        raise RuntimeError("Repackaged ZIP failed verification: %s" % "; ".join(problems))
    progress.log("packaged %s" % zip_path.name)

    progress.skip("Packaging world ZIP (skymaps)", "kept (--only)")
    progress.skip("Region ZIPs", "kept (--only)")

    if not args.keep_staging:
        shutil.rmtree(stage_dir, ignore_errors=True)

    # 9) publish ---------------------------------------------------------------------
    if args.publish:
        progress.step("Publishing to GitHub release", "checking release")
        # One asset of several is replaced, so the release's notes (which list
        # all of them) are left as they are.
        publish([zip_path], cycle_code, cycle_display, progress.log,
                clobber=args.clobber, update_notes=False)
    else:
        progress.skip("Publishing to GitHub release", "not requested (--publish)")

    progress.step("Done", "")
    progress.finish("done")
    print("Done: regenerated %s for AIRAC %s (%s)"
          % (", ".join(sorted(only)), cycle_display, cycle_code))
    if not args.publish:
        print("Nothing was uploaded. Run again with --publish to upload %s." % zip_path.name)
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


def render_line(frac: float, bar: str, name: str, detail: str,
                elapsed: str, left: str, width: int) -> str:
    """One progress line, exactly ``width`` characters wide.

    The percent, the bar and the two timers are always shown. The step name
    and its detail take what is left and are cut short when it is not enough.
    """
    head = "%3.0f%% [%s] " % (frac * 100, bar)
    tail = " | %s elapsed | %s left" % (elapsed, left)
    text = name + ((" - " + detail) if detail else "")
    room = width - len(head) - len(tail)
    if room < 0:
        return (head + tail.lstrip(" |"))[:width].ljust(width)
    return (head + text[:room] + tail).ljust(width)


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

    def skip(self, name: str, detail: str = "skipped") -> None:
        """A planned step that does not run: its weight counts as done."""
        self.step(name, detail)
        self.advance(1.0)

    def advance(self, fraction: float, detail: str = "") -> None:
        with self._lock:
            self._step_frac = max(0.0, min(1.0, fraction))
            if detail:
                self._detail = detail
            self._render()

    def log(self, message: str) -> None:
        with self._lock:
            line = " " * (self._line_len + 2)
            sys.stdout.write("\r" + line + "\r" + pack.scrub_paths(message) + "\n")
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
        # Never exceed the console width: a wrapped line defeats the \r. The
        # timers keep their place and the step text gives way.
        width = max(20, shutil.get_terminal_size((100, 24)).columns - 1)
        line = render_line(frac, bar, name, detail, _clock(elapsed), _clock(eta), width)
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
        "- `EG_<cycle>.zip` or `<cycle>.zip` - the combined package SkyRadar installs",
        "- `ALL_<cycle>.zip` - world package for skymaps",
        "- `<REGION>_<cycle>.zip` - per-region subsets derived automatically",
        "",
        "Sources: NATS UK eAIP, X-Plane Scenery Gateway, openAIP daily exports "
        "(https://www.openaip.net, CC BY-NC 4.0), OurAirports (public domain).",
        "",
    ]
    path = out_dir / ("release-notes-%s.md" % cycle_code)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def release_asset_names(tag: str):
    """Asset names on the release, or None when the release does not exist."""
    view = subprocess.run(
        ["gh", "release", "view", tag, "--repo", NAVDATA_REPO,
         "--json", "assets", "--jq", ".assets[].name"],
        capture_output=True, text=True)
    if view.returncode != 0:
        # "release not found" is the expected signal to create it; anything
        # else (no network, not signed in) must not be read as "create".
        if "not found" in (view.stderr or "").lower():
            return None
        raise RuntimeError("gh release view failed: %s" % (view.stderr or "").strip())
    return [name for name in view.stdout.splitlines() if name.strip()]


def combined_asset_names(cycle_code: str) -> set:
    """Both names the combined navdata can be published under for a cycle.

    SkyRadar takes ``EG_<cycle>.zip`` when the release has it and
    ``<cycle>.zip`` otherwise, so a release must never hold the two at once:
    the one that is not being replaced would be stale, and may be the one
    that gets installed.
    """
    return {pack.asset_name(REGION, cycle_code), "%s.zip" % cycle_code}


def publish(zips: list, cycle_code: str, cycle_display: str, log,
            clobber: bool = False, update_notes: bool = True) -> None:
    """Upload the ZIPs to the cycle's GitHub release.

    Assets already on the release are left alone and the upload is refused
    unless ``clobber`` is set. The combined navdata under its other name
    (``<cycle>.zip`` against ``EG_<cycle>.zip``) counts as already there:
    with ``clobber`` it is deleted once the new one is up. ``update_notes`` is
    off when only some of the release's assets are being uploaded, so its
    notes keep listing all of them.
    """
    tag = "airac-%s" % cycle_code
    title = "AIRAC %s - SkyHigh navdata" % cycle_display
    if not zips:
        raise RuntimeError("No release ZIPs found")
    zips = [Path(z) for z in zips]
    names = {z.name for z in zips}
    combined = combined_asset_names(cycle_code)
    if len(names & combined) > 1:
        raise RuntimeError(
            "Both %s would be uploaded. A release holds the combined navdata under "
            "one name only." % " and ".join(sorted(names & combined)))
    existing = release_asset_names(tag)
    if existing is not None:
        taken = sorted(set(existing) & names)
        # the combined navdata under the name this run does not upload
        obsolete = sorted((combined - names) & set(existing)) if names & combined else []
        if (taken or obsolete) and not clobber:
            raise RuntimeError(
                "Release %s already has %s. Nothing was uploaded; pass --clobber to "
                "replace the live asset(s)%s." % (
                    tag, ", ".join(taken + obsolete),
                    " and delete %s" % ", ".join(obsolete) if obsolete else ""))
        log("Release %s exists - uploading %d asset(s)%s"
            % (tag, len(zips), " (replacing %d)" % len(taken) if taken else ""))
        extra = ["--clobber"] if clobber else []
        for batch in _chunks([str(z) for z in zips], 20):
            _gh("release", "upload", tag, "--repo", NAVDATA_REPO, *extra, *batch)
        # After the upload, so the release is never without the combined navdata.
        for name in obsolete:
            _gh("release", "delete-asset", tag, name, "--repo", NAVDATA_REPO, "--yes")
            log("Deleted %s from the release (replaced by %s)"
                % (name, ", ".join(sorted(names & combined))))
        if obsolete and not update_notes:
            log("The release notes still name %s; edit them on the release."
                % ", ".join(obsolete))
        if update_notes:
            notes = write_master_notes(zips[0].parent, cycle_display, cycle_code, zips)
            _gh("release", "edit", tag, "--repo", NAVDATA_REPO,
                "--title", title, "--notes-file", str(notes))
    else:
        notes = write_master_notes(zips[0].parent, cycle_display, cycle_code, zips)
        log("Creating release %s with %d asset(s)" % (tag, len(zips)))
        first, rest = [str(z) for z in zips[:20]], [str(z) for z in zips[20:]]
        _gh("release", "create", tag, "--repo", NAVDATA_REPO,
            "--title", title, "--notes-file", str(notes), "--latest", *first)
        for batch in _chunks(rest, 20):
            _gh("release", "upload", tag, "--repo", NAVDATA_REPO, *batch)
    log("Published: https://github.com/%s/releases/tag/%s" % (NAVDATA_REPO, tag))


# --- Pipeline ----------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="make_airac.py",
        description="Build an AIRAC navdata release end to end. Uploads only with --publish.")
    parser.add_argument("--airac", default=None,
                        help="Override the detected AIRAC cycle, e.g. 2610.")
    parser.add_argument("--skip-layouts", action="store_true",
                        help="Skip the X-Plane Gateway airport layouts.")
    parser.add_argument("--no-base", action="store_true",
                        help="Skip the global base data (openAIP + OurAirports).")
    parser.add_argument("--regions", default=None,
                        help="'auto' (default), a comma list of ICAO prefixes, or 'none'.")
    parser.add_argument("--min-region-airports", type=int, default=10)
    parser.add_argument("--airspace-source", choices=("both", "eaip", "openaip"),
                        default="both",
                        help="Airspace drawings. 'both' ships the NATS eAIP drawings "
                             "(CONFIG/bg/ENR21 and the per-aerodrome groups, read by "
                             "the skymaps chart build) and the openAIP per-country "
                             "groups (CONFIG/bg/OAIP<ISO>.lst with OAIP-countries, "
                             "read by SkyRadar). 'eaip' or 'openaip' ships one of "
                             "them. The choice applies to every ZIP the build writes. "
                             "Default: %(default)s.")
    parser.add_argument("--airspace-scope", choices=("world", "eg"), default="world",
                        help="Scope of the openAIP groups: 'world' (every openAIP "
                             "country) or 'eg' (UK and Ireland only). "
                             "Default: %(default)s.")
    parser.add_argument("--layouts", default="eg",
                        help="Aerodrome layout scope: 'eg' (the eAIP aerodromes, "
                             "~116 layouts), 'world' (every aerodrome in the merged "
                             "navdata - 40k+ Gateway requests, hours) or a "
                             "comma-separated list of ICAO region prefixes to add/"
                             "refresh on top of an existing set (e.g. EG,LF or ED). "
                             "Default: %(default)s.")
    parser.add_argument("--publish", action="store_true",
                        help="After building, upload the ZIPs to the GitHub release "
                             "(tag airac-<cycle>). Without this nothing is uploaded.")
    parser.add_argument("--clobber", action="store_true",
                        help="With --publish, replace assets that are already on the "
                             "release. SkyRadar installs pull from it.")
    # Accepted so older command lines keep working. Not publishing is the default.
    parser.add_argument("--no-publish", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--single", action="store_true",
                        help="Build ONE asset per cycle: <cycle>.zip (e.g. 2611.zip) "
                             "containing everything - the global base merged under the "
                             "SkyHigh data. No world/region ZIPs.")
    parser.add_argument("--only", default=None,
                        help="Regenerate only selected parts of an existing navdata, "
                             "comma-separated from: download, fixes, navaids, airways, "
                             "ad-positions, entry-exit, ctr, layouts, airspace. "
                             "Everything else is kept from the seed ZIP (--from-zip, "
                             "default: the newest combined ZIP in --out). The base "
                             "merge always re-runs after any nav regeneration.")
    parser.add_argument("--from-zip", default=None,
                        help="Seed the staging tree from this existing combined navdata "
                             "ZIP (used with --only). Default: the newest EG_<cycle>.zip "
                             "or <cycle>.zip in --out.")
    parser.add_argument("--keep-staging", action="store_true",
                        help="Keep the intermediate staging tree for debugging.")
    parser.add_argument("--out", default="dist")
    parser.add_argument("--cache-dir", default=".cache")
    args = parser.parse_args(argv)
    if args.publish and args.no_publish:
        parser.error("--publish and --no-publish cannot be used together.")
    if args.clobber and not args.publish:
        parser.error("--clobber only applies with --publish.")
    if args.no_base and args.airspace_source == "openaip":
        parser.error("--airspace-source openaip needs the base data (drop --no-base).")
    if args.layouts not in ("eg", "world"):
        try:
            _parse_layout_regions(args.layouts)
        except ValueError as error:
            parser.error(str(error))
    return args


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

        # 3) global base ------------------------------------------------------
        base_data = None
        if args.no_base:
            progress.skip("openAIP global exports", "skipped (--no-base)")
            progress.skip("OurAirports CSV", "skipped (--no-base)")
        else:
            progress.step("openAIP global exports", "listing")
            openaip = base.load_openaip(
                Path(args.cache_dir), log=None,
                progress_cb=lambda done, total, label:
                    progress.advance(done / max(total, 1), label))
            progress.step("OurAirports CSV", "")
            ourairports = base.load_ourairports(Path(args.cache_dir), log=progress.log)
            # Raises when either source came back empty: nothing is packaged
            # or published from a build that is missing the base it asked for.
            base_data = base.require_base(openaip, ourairports)
            counts.update(ourairports.get("counts", {}))
            counts.update(openaip.get("counts", {}))

        # 4) airspace, overrides, merge ------------------------------------------
        # Same order as build_navdata.py: the hand-maintained overrides go in
        # first and the base merges underneath them, so an overridden nav file
        # still gets the base rows it does not have. The merge runs before the
        # layouts because worldwide layouts need the merged aerodrome list.
        progress.step("Merging base + openAIP airspace", "")
        draw_eaip, draw_openaip = airspace_plan(args.airspace_source, bool(base_data))
        if draw_eaip:
            counts.update(build_airspace_from_html(
                html_dir, bg_dir, region, cycle_display, log=None))
        if draw_openaip:
            if not base_data.get("airspaces"):
                raise RuntimeError("openAIP returned no airspace.")
            counts["airspace_drawings"] = regions.write_openaip_airspace(
                bg_dir, base_data, args.airspace_scope, cycle_display, progress.log)
        applied = []
        if OVERRIDES_DIR.is_dir():
            applied = pack.apply_overrides(stage_dir, OVERRIDES_DIR, log=None)
        counts["overrides"] = len(applied)
        if base_data:
            counts.update(merge.merge_base_into_stage(stage_dir, base_data, log=None))
        progress.advance(1.0)

        # 5) airport layouts ----------------------------------------------------
        # Aerodromes with a CONFIG/ad override are skipped by the download, so
        # the override copied in above stays as written.
        layouts_source = ""
        if args.skip_layouts:
            progress.skip("X-Plane Gateway airport layouts", "skipped (--skip-layouts)")
            counts["layouts"] = 0
        else:
            progress.step("X-Plane Gateway airport layouts", "")
            positions = stage_dir / "CONFIG" / "nav" / "AD-positions"
            icaos = _layout_icaos(args.layouts, positions, icaos)
            if args.layouts == "world":
                # the Gateway 404s aerodromes it does not know, so expect a
                # large 'unavailable' count and a multi-hour run
                progress.log("worldwide layouts: %d aerodrome(s) requested" % len(icaos))
            else:
                progress.log("layouts %s: %d aerodrome(s) requested" % (args.layouts, len(icaos)))
            custom_icaos = pack.custom_ad_icaos(OVERRIDES_DIR)
            summary = aip.download_all_ad_layouts(
                icaos, ad_dir, log=None, skip=custom_icaos,
                progress_cb=lambda done, total, icao:
                    progress.advance(done / max(total, 1),
                                     "%s (%d/%d)" % (icao, done, total)))
            counts["layouts"] = summary["downloaded"]
            counts["layouts_custom"] = summary["custom"]
            counts["layouts_unavailable"] = summary["unavailable"]
            counts["layouts_failed"] = summary["failed"]
            layouts_source = "X-Plane Gateway (https://gateway.x-plane.com) with local overrides"

        (nav_dir / "airac.txt").write_text(
            "region: %s\nairac: %s\nairac_code: %s\nbuilt_utc: %s\ngenerator: %s %s\n" % (
                region, cycle_display, cycle_code,
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                GENERATOR_NAME, __version__),
            encoding="utf-8")

        # 6) the release ZIP/ZIPs --------------------------------------------
        # Single mode: one <cycle>.zip with everything in it (the manifest
        # keeps region EG; SkyRadar looks for EG_<cycle>.zip first and falls
        # back to <cycle>.zip).
        single = args.single
        progress.step("Packaging the release ZIP", "")
        sources = build_sources(
            base_data, layouts_source, len(applied),
            airspace="both" if draw_eaip and draw_openaip else
                     "eaip" if draw_eaip else "openaip")
        manifest = sealed_manifest(
            stage_dir, region=region, airac=cycle_display, airac_code=cycle_code,
            sources=sources, counts=counts, overrides=applied,
            generator=GENERATOR_NAME, generator_version=__version__)
        asset_name = ("%s.zip" % cycle_code) if single else pack.asset_name(region, cycle_code)
        eg_zip = pack.make_zip(stage_dir, out_dir / asset_name)
        problems = pack.verify_zip(eg_zip, manifest, counts)
        if problems:
            raise RuntimeError("Combined ZIP failed verification: %s" % "; ".join(problems))
        if not single:
            pack.write_release_notes(eg_zip.with_suffix(".md"), manifest)

        # 7) world ZIP ----------------------------------------------------------
        # Needs the global base: without it the tree is the UK build alone.
        world_zip = None
        if single:
            progress.skip("Packaging world ZIP (skymaps)", "skipped (--single)")
        elif not base_data:
            progress.skip("Packaging world ZIP (skymaps)", "skipped (no base data)")
        else:
            progress.step("Packaging world ZIP (skymaps)", "")
            world_zip = regions.build_global_zip(
                stage_dir, out_dir, cycle_display, cycle_code,
                generator_version=__version__, log=progress.log,
                sources=sources, counts=counts)

        # 8) region ZIPs --------------------------------------------------------
        region_zips = []
        region_spec = (args.regions or "").strip().lower()
        if single:
            progress.skip("Region ZIPs", "skipped (--single)")
        elif region_spec == "none":
            progress.skip("Region ZIPs", "skipped (--regions none)")
        elif not base_data:
            progress.skip("Region ZIPs", "skipped (no base data)")
        else:
            chosen = regions.resolve_regions(args.regions, stage_dir, args.min_region_airports)
            progress.step("Region ZIPs", "0/%d" % len(chosen))
            for index, prefix in enumerate(chosen, start=1):
                progress.advance(index / max(len(chosen), 1), prefix)
                region_zips.append(regions.build_region_zip(
                    prefix, stage_dir, out_dir, cycle_display, cycle_code,
                    base_airspaces=base_data.get("airspaces") if draw_openaip else None,
                    generator_version=__version__, sources=sources))
            progress.advance(1.0)

        if not args.keep_staging:
            shutil.rmtree(stage_dir, ignore_errors=True)

        # 9) publish ------------------------------------------------------------
        # Only the ZIPs this run built are uploaded, never whatever is in dist.
        release_zips = [eg_zip]
        if world_zip:
            release_zips.append(world_zip)
        release_zips.extend(z for z in region_zips if z)
        if args.publish:
            progress.step("Publishing to GitHub release", "checking release")
            publish(release_zips, cycle_code, cycle_display, progress.log,
                    clobber=args.clobber)
        else:
            progress.skip("Publishing to GitHub release", "not requested (--publish)")

        progress.step("Done", "")
        progress.finish("done")
        print("Done: AIRAC %s (%s) in %s"
              % (cycle_display, cycle_code, pack.display_path(out_dir)))
        if not args.publish:
            print("Nothing was uploaded. Run again with --publish to upload to the release.")
        return 0
    except KeyboardInterrupt:
        progress.finish("STOPPED")
        print("Stopped by user.", file=sys.stderr)
        return 130
    except Exception as error:  # noqa: BLE001 - surface after closing the bar
        progress.finish("FAILED")
        print("FAILED: %s" % pack.error_text(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
