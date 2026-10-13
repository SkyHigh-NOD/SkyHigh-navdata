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
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from skyhigh_navdata import GENERATOR_NAME, REGION, __version__
from skyhigh_navdata import aip, pack
from skyhigh_navdata.airspace import build_airspace_from_html
from skyhigh_navdata.build import build_navdata_from_html
from skyhigh_navdata.cycle import CycleError, normalize

SUPPORTED_REGIONS = (REGION,)


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
                        help="Skip the ENR 2.1 / AD 2.17 airspace drawings (CONFIG/bg).")
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
    if args.skip_airspace:
        log("Skipping airspace drawings (--skip-airspace).")
        sources["airspace"] = "skipped"
    else:
        log("Building airspace drawings into CONFIG/bg ...")
        counts.update(build_airspace_from_html(html_dir, bg_dir, region, cycle_display, log=log))
        sources["airspace"] = "NATS UK eAIP ENR 2.1 / AD 2.17"
    if args.skip_layouts:
        log("Skipping X-Plane Gateway airport layouts (--skip-layouts).")
        sources["layouts"] = "skipped"
    else:
        log("Downloading %d airport layouts from the X-Plane Gateway ..." % len(icaos))
        layout_summary = aip.download_all_ad_layouts(icaos, ad_dir, log=log)
        counts["layouts"] = layout_summary["downloaded"]
        counts["layouts_unavailable"] = layout_summary["unavailable"]
        counts["layouts_failed"] = layout_summary["failed"]
        sources["layouts"] = "X-Plane Gateway (https://gateway.x-plane.com)"

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
        sources=sources, counts=counts,
        generator=GENERATOR_NAME, generator_version=__version__,
    )
    pack.write_manifest(stage_dir, manifest)

    out_name = args.name or pack.asset_name(region, cycle_code)
    zip_path = pack.make_zip(stage_dir, out_dir / out_name)

    if not args.keep_staging:
        shutil.rmtree(stage_dir, ignore_errors=True)

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print("", flush=True)
    print("Built %s" % zip_path, flush=True)
    print("  AIRAC cycle : %s (%s)" % (cycle_display, cycle_code), flush=True)
    print("  Files       : %d (%.2f MB)" % (len(manifest["files"]), size_mb), flush=True)
    print("  Counts      : %s" % ", ".join("%s=%s" % item for item in sorted(counts.items())), flush=True)
    print("", flush=True)
    print("Next: upload this ZIP as an asset of a GitHub release (e.g. tag 'airac-%s')." % cycle_code, flush=True)
    print("SkyRadar installs the newest asset from the repo's latest release.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
