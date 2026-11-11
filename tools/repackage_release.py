"""Repackage a published combined navdata ZIP through the current pipeline.

Given the released ``EG_<airac>.zip`` for a cycle, this regenerates everything
the *current* build would emit for that cycle without re-downloading the eAIP:

- the global nav base (openAIP + OurAirports) merged underneath the SkyHigh data,
- the openAIP airspace drawings replacing the eAIP-generated airspace,
- the world ZIP (``ALL_<airac>.zip``) for skymaps,
- one ZIP per ICAO region (``LF_<airac>.zip`` ...).

Usage:
    python tools/repackage_release.py dist/EG_2610.zip [--regions auto|none|LF,ED,...]
        [--min-region-airports 10] [--out dist] [--cache .cache]

Used to backfill release assets for the running cycle after a builder change.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skyhigh_navdata import base, merge, regions


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="repackage_release.py",
        description="Rebuild world + region release ZIPs from a published combined ZIP.")
    parser.add_argument("zip", help="Path to the published combined ZIP, e.g. EG_2610.zip")
    parser.add_argument("--regions", default=None,
                        help="'auto' (default), a comma list of ICAO prefixes, or 'none'.")
    parser.add_argument("--min-region-airports", type=int, default=10)
    parser.add_argument("--out", default="dist")
    parser.add_argument("--cache", default=".cache")
    parser.add_argument("--airac", default=None,
                        help="Cycle display '10/26'; default read from the manifest.")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    log = print
    source = Path(args.zip)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = {}
    with zipfile.ZipFile(source) as archive:
        manifest = __import__("json").loads(archive.read("manifest.json").decode("utf-8"))
    airac_display = args.airac or str(manifest.get("airac") or "")
    airac_code = str(manifest.get("airac_code") or "")
    stage = out_dir / (".staging-repackage-%s" % airac_code)
    if stage.exists():
        shutil.rmtree(stage)
    with zipfile.ZipFile(source) as archive:
        archive.extractall(stage)
    log("Repackaging %s (AIRAC %s) ..." % (source.name, airac_display or airac_code))

    ourairports = base.load_ourairports(Path(args.cache), log=log)
    openaip = base.load_openaip(Path(args.cache), log=log)
    base_data = dict(openaip)
    base_data["ourairports"] = ourairports
    merge.merge_base_into_stage(stage, base_data, log=log)

    # openAIP airspace replaces whatever airspace the published ZIP carried.
    bg_dir = stage / "CONFIG" / "bg"
    if bg_dir.exists():
        shutil.rmtree(bg_dir)
    if base_data.get("airspaces"):
        added, _group = regions.write_openaip_airspace_group(
            bg_dir, base_data["airspaces"],
            regions.EG_REGION["bbox"], airac_display or airac_code,
            countries=regions.EG_REGION["countries"])
        log("airspace: %d openAIP drawing(s) for the EG scope" % added)

    world = regions.build_global_zip(
        stage, out_dir, airac_display, airac_code, log=log)
    log("Built %s" % world.name)

    region_spec = (args.regions or "").strip().lower()
    if region_spec != "none":
        for prefix in regions.resolve_regions(args.regions, stage, args.min_region_airports):
            regions.build_region_zip(
                prefix, stage, out_dir, airac_display, airac_code,
                base_airspaces=base_data.get("airspaces"), log=log)

    shutil.rmtree(stage, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
