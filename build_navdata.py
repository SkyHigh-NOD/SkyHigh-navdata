#!/usr/bin/env python3
"""Build a SkyRadar navdata release ZIP.

Downloads the current NATS UK AIRAC archive, parses it into SkyRadar's
``CONFIG/nav`` files, fetches X-Plane Gateway airport layouts into
``CONFIG/ad``, writes a ``manifest.json`` and zips the whole thing into a
single release asset named after the FIR and the AIRAC cycle, e.g.
``EG_2610.zip``.

Examples
--------
    python build_navdata.py                      # full build from live sources
    python build_navdata.py --skip-layouts       # nav files only (fast)
    python build_navdata.py --airac 2610 --source-dir ./html   # offline

Upload the produced ZIP to a GitHub release. Each cycle has its own asset name;
SkyRadar finds the newest one from the repository's latest release.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import urllib.error
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from skyhigh_navdata import aip, GENERATOR_NAME, REGION, __version__
from skyhigh_navdata import base, firs, merge, pack
from skyhigh_navdata.airspace import build_airspace_from_html
from skyhigh_navdata.build import build_navdata_from_html
from skyhigh_navdata.cycle import CycleError, normalize

SUPPORTED_REGIONS = (REGION,)

# Hand-maintained files kept in the repository's CONFIG/ folder override
# anything the build generates (customised sector airspace, airport layouts,
# navaids, ...), and are shipped in the release exactly as written.
DEFAULT_OVERRIDES_DIR = Path(__file__).resolve().parent / "CONFIG"


def make_logger(quiet: bool = False):
    def log(message: str) -> None:
        if not quiet:
            print(message, flush=True)
    return log


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="build_navdata.py",
        description="Build a SkyRadar navdata release ZIP (FIR_<airac>.zip).",
    )
    parser.add_argument("--region", default=REGION,
                        help="FIR prefix to build (only EG is supported). Default: %(default)s")
    parser.add_argument("--airac", default=None,
                        help="AIRAC cycle, e.g. 2610 or 10/26. Default: detect from NATS.")
    parser.add_argument("--source-dir", default=None,
                        help="Use an already-extracted eAIP HTML directory instead of downloading NATS.")
    parser.add_argument("--airac-zip", default=None,
                        help="Use a local NATS AIRAC ZIP instead of downloading it.")
    parser.add_argument("--skip-layouts", action="store_true",
                        help="Skip the X-Plane Gateway airport layouts (CONFIG/ad).")
    parser.add_argument("--skip-airspace", action="store_true",
                        help="Skip the airspace drawings (CONFIG/bg) entirely.")
    parser.add_argument("--airspace-source", choices=("openaip", "eaip"), default="openaip",
                        help="Airspace drawing source. openAIP replaces the (currently "
                             "broken) eAIP-generated airspace; 'eaip' restores the old "
                             "NATS eAIP ENR 2.1 / AD 2.17 parser. Default: %(default)s. "
                             "Requires --no-base to be off for 'openaip'.")
    parser.add_argument("--no-base", action="store_true",
                        help="Do not merge the global base data (openAIP + OurAirports) "
                             "under the SkyHigh data.")
    parser.add_argument("--firs", default=None,
                        help="Comma-separated FIR ids to also package individually, "
                             "e.g. 'EGTT,EISN' (default: every FIR in FIRS). "
                             "Pass 'none' to build only the combined ZIP.")
    parser.add_argument("--cache-dir", default=".cache",
                        help="Where base-data downloads are cached. Default: %(default)s")
    parser.add_argument("--overrides-dir", default=None,
                        help="CONFIG folder of hand-maintained files that override generated "
                             "ones. Default: the repository's CONFIG/ folder.")
    parser.add_argument("--no-overrides", action="store_true",
                        help="Ignore the local CONFIG/ overrides.")
    parser.add_argument("--notes-out", default=None,
                        help="Where to write the release notes. Default: beside the ZIP, "
                             "as <zip>.md")
    parser.add_argument("--no-notes", action="store_true",
                        help="Do not write the release notes file.")
    parser.add_argument("--out", default="dist",
                        help="Output directory for the release ZIP. Default: %(default)s")
    parser.add_argument("--name", default=None,
                        help="Output ZIP file name. Default: <region>_<airac>.zip")
    parser.add_argument("--work-dir", default=None,
                        help="Staging directory. Default: <out>/.staging-<region>")
    parser.add_argument("--keep-staging", action="store_true",
                        help="Do not delete the staging directory after building.")
    parser.add_argument("--quiet", action="store_true", help="Only print the final summary.")
    return parser.parse_args(argv)


def resolve_sources(args, stage_dir, log):
    """Return (html_dir, cycle_display, cycle_code) from the requested source."""
    requested = None
    if args.airac:
        try:
            requested = normalize(args.airac)
        except CycleError as error:
            raise SystemExit(str(error))

    if args.source_dir:
        html_dir = Path(args.source_dir)
        if not html_dir.is_dir():
            raise SystemExit("--source-dir is not a directory: %s" % html_dir)
        log("Using pre-extracted eAIP HTML from %s" % html_dir)
        cycle = requested
    elif args.airac_zip:
        archive_path = Path(args.airac_zip)
        if not archive_path.is_file():
            raise SystemExit("--airac-zip is not a file: %s" % archive_path)
        html_dir = stage_dir.parent / "airac-html"
        if html_dir.exists():
            shutil.rmtree(html_dir)
        html_dir.mkdir(parents=True, exist_ok=True)
        log("Extracting local AIRAC ZIP %s" % archive_path)
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(html_dir)
        cycle = requested
    else:
        detected, html_dir = aip.download_and_extract_airac(stage_dir.parent / "airac", log=log)
        if requested:
            if requested[0] != detected_code(detected):
                log("Overriding NATS-detected cycle %s with %s" % (detected, requested[1]))
            cycle = requested
        else:
            cycle = normalize(detected)

    if cycle is None:
        raise SystemExit("--airac is required when the source is a local ZIP or HTML directory.")
    return html_dir, cycle[1], cycle[0]


def detected_code(detected_display: str) -> str:
    try:
        return normalize(detected_display)[0]
    except CycleError:
        return ""


def main(argv=None) -> int:
    args = parse_args(argv)
    log = make_logger(args.quiet)
    region = args.region.upper()
    if region not in SUPPORTED_REGIONS:
        raise SystemExit("Unsupported region %r; supported: %s" % (region, ", ".join(SUPPORTED_REGIONS)))

    out_dir = Path(args.out)
    stage_dir = Path(args.work_dir) if args.work_dir else out_dir / (".staging-%s" % region)
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)

    if args.no_overrides:
        overrides_dir = None
    elif args.overrides_dir:
        overrides_dir = Path(args.overrides_dir)
        if not overrides_dir.is_dir():
            raise SystemExit("--overrides-dir is not a directory: %s" % overrides_dir)
    else:
        overrides_dir = DEFAULT_OVERRIDES_DIR if DEFAULT_OVERRIDES_DIR.is_dir() else None

    nav_dir = stage_dir / "CONFIG" / "nav"
    ad_dir = stage_dir / "CONFIG" / "ad"
    bg_dir = stage_dir / "CONFIG" / "bg"

    log("%s %s - building navdata for %s" % (GENERATOR_NAME, __version__, region))
    html_dir, cycle_display, cycle_code = resolve_sources(args, stage_dir, log)
    log("AIRAC cycle: %s (%s)" % (cycle_display, cycle_code))

    log("Parsing eAIP HTML into CONFIG/nav ...")
    result = build_navdata_from_html(html_dir, nav_dir, region, cycle_display, log=log)
    counts = dict(result["counts"])
    icaos = result["icaos"]

    sources = {"aip": "NATS UK eAIP (https://nats-uk.ead-it.com)"}

    # Global base data loads before the airspace step: when openAIP provides
    # the airspace drawings (the eAIP-generated ones are currently broken),
    # they come straight from the freshly loaded exports.
    base_data = None
    if not args.no_base:
        log("Loading global base data (openAIP + OurAirports) ...")
        try:
            ourairports = base.load_ourairports(Path(args.cache_dir), log=log)
            openaip = base.load_openaip(Path(args.cache_dir), log=log)
            base_data = dict(openaip)
            base_data["ourairports"] = ourairports
            counts.update(ourairports.get("counts", {}))
            counts.update(openaip.get("counts", {}))
            sources["base"] = ("openAIP daily exports (https://storage.openaip.net) and "
                               "OurAirports (https://ourairports-data.github.io) — global "
                               "base under the SkyHigh data")
        except (urllib.error.URLError, OSError) as error:
            log("WARNING: base data unavailable (%s); building SkyHigh data only." % error)
            base_data = None

    if args.skip_airspace:
        log("Skipping airspace drawings (--skip-airspace).")
        sources["airspace"] = "skipped"
    elif args.airspace_source == "openaip":
        if not base_data:
            log("WARNING: no openAIP data loaded (--no-base or fetch failed); "
                "CONFIG/bg will have no airspace drawings.")
            sources["airspace"] = "unavailable (no openAIP data)"
        else:
            log("Building airspace drawings from openAIP exports "
                "(replacing the broken eAIP airspace) ...")
            added, _group = firs.write_openaip_airspace_group(
                bg_dir, base_data.get("airspaces", []),
                firs.EG_REGION["bbox"], cycle_display,
                countries=firs.EG_REGION["countries"])
            counts["airspace_drawings"] = added
            sources["airspace"] = "openAIP daily exports (replaces eAIP airspace)"
    else:
        log("Building airspace drawings into CONFIG/bg ...")
        counts.update(build_airspace_from_html(html_dir, bg_dir, region, cycle_display, log=log))
        sources["airspace"] = "NATS UK eAIP ENR 2.1 / AD 2.17"
    if args.skip_layouts:
        log("Skipping X-Plane Gateway airport layouts (--skip-layouts).")
        sources["layouts"] = "skipped"
    else:
        custom_icaos = pack.custom_ad_icaos(overrides_dir)
        if custom_icaos:
            log("Using local CONFIG/ad overrides for: %s" % ", ".join(sorted(custom_icaos)))
        log("Downloading %d airport layouts from the X-Plane Gateway ..." % len(icaos))
        layout_summary = aip.download_all_ad_layouts(icaos, ad_dir, log=log, skip=custom_icaos)
        counts["layouts"] = layout_summary["downloaded"]
        counts["layouts_custom"] = layout_summary["custom"]
        counts["layouts_unavailable"] = layout_summary["unavailable"]
        counts["layouts_failed"] = layout_summary["failed"]
        sources["layouts"] = "X-Plane Gateway (https://gateway.x-plane.com) with local overrides"

    applied = []
    if overrides_dir:
        log("Applying local overrides from %s ..." % overrides_dir)
        applied = pack.apply_overrides(stage_dir, overrides_dir, log=log)
        counts["overrides"] = len(applied)
        sources["overrides"] = ("%d file(s) from %s" % (len(applied), overrides_dir)
                                if applied else "none")
    else:
        sources["overrides"] = "none"

    # Global base data merges underneath the SkyHigh files: the eAIP-generated
    # entries already sit in the nav files, so the merge only ever appends
    # identities the build has not produced — SkyHigh > openAIP > OurAirports.
    if base_data:
        counts.update(merge.merge_base_into_stage(stage_dir, base_data, log=log))

    (nav_dir / "airac.txt").write_text(
        "region: %s\nairac: %s\nairac_code: %s\nbuilt_utc: %s\ngenerator: %s %s\n" % (
            region, cycle_display, cycle_code,
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            GENERATOR_NAME, __version__,
        ),
        encoding="utf-8",
    )

    manifest = pack.build_manifest(
        stage_dir, region=region, airac=cycle_display, airac_code=cycle_code,
        sources=sources, counts=counts, overrides=applied,
        generator=GENERATOR_NAME, generator_version=__version__,
    )
    pack.write_manifest(stage_dir, manifest)

    out_name = args.name or pack.asset_name(region, cycle_code)
    zip_path = pack.make_zip(stage_dir, out_dir / out_name)

    problems = pack.verify_zip(zip_path, manifest, counts)
    if problems:
        raise RuntimeError(
            "Built ZIP failed its consistency check:\n  - %s\n"
            "Files the build reported writing are absent from %s (usually a real-time "
            "antivirus scanner). Add the build folder to your antivirus exclusions, then "
            "rebuild." % ("\n  - ".join(problems), stage_dir))

    # Per-FIR subsets of the finished tree (the combined ZIP is untouched).
    fir_zips = []
    fir_spec = (args.firs or "").strip().lower()
    if fir_spec != "none":
        chosen = firs.resolve_firs(args.firs if args.firs else None)
        for fir_id, fir_def in chosen.items():
            fir_zips.append(firs.build_fir_zip(
                fir_id, fir_def, stage_dir, out_dir,
                cycle_display, cycle_code,
                base_airspaces=(base_data or {}).get("airspaces"),
                generator_version=__version__,
                log=log,
            ))

    if not args.keep_staging:
        shutil.rmtree(stage_dir, ignore_errors=True)

    notes_path = None
    if not args.no_notes:
        notes_path = Path(args.notes_out) if args.notes_out else zip_path.with_suffix(".md")
        pack.write_release_notes(notes_path, manifest)

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print("", flush=True)
    print("Built %s" % zip_path, flush=True)
    print("  AIRAC cycle : %s (%s)" % (cycle_display, cycle_code), flush=True)
    print("  Files       : %d (%.2f MB)" % (len(manifest["files"]), size_mb), flush=True)
    print("  Counts      : %s" % ", ".join("%s=%s" % item for item in sorted(counts.items())), flush=True)
    for fir_zip in fir_zips:
        if fir_zip:
            print("  FIR release : %s" % fir_zip.name, flush=True)
    if notes_path:
        print("  Notes       : %s" % notes_path, flush=True)
    print("", flush=True)
    print("Next: upload this ZIP as an asset of a GitHub release (e.g. tag 'airac-%s')." % cycle_code, flush=True)
    print("SkyRadar installs the newest asset from the repo's latest release.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
