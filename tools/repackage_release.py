"""Repackage a published combined navdata ZIP through the current pipeline.

Given the released ``EG_<airac>.zip`` for a cycle, this regenerates everything
the *current* build would emit for that cycle without re-downloading the eAIP:

- the global nav base (openAIP + OurAirports) merged underneath the SkyHigh
  data. The base rows the published ZIP already carries are taken out first,
  so the merge is the same one a fresh build does,
- with ``--airspace-source openaip``, fresh openAIP per-country airspace
  groups (``OAIP<ISO>``) beside the eAIP drawings the published ZIP carried,
  which are always kept (the default leaves all airspace as published, in the
  region ZIPs too),
- the world ZIP (``ALL_<airac>.zip``) for skymaps,
- one ZIP per ICAO region (``LF_<airac>.zip`` ...).

Usage:
    python tools/repackage_release.py dist/EG_2610.zip [--regions auto|none|LF,ED,...]
        [--min-region-airports 10] [--out dist] [--cache .cache]
        [--airspace-source keep|openaip]

Nothing is uploaded: the ZIPs are written to ``--out``.

Used to backfill release assets for the running cycle after a builder change.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skyhigh_navdata import REGION, base, merge, pack, regions

OPENAIP_AIRSPACE = "openAIP daily exports (CC BY-NC 4.0)"


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
    parser.add_argument("--airspace-source", choices=("keep", "openaip"), default="keep",
                        help="'keep' (default) leaves the published ZIP's airspace as "
                             "it is; 'openaip' rewrites the openAIP per-country groups "
                             "and keeps the eAIP drawings.")
    return parser.parse_args(argv)


def airspace_label(previous: str) -> str:
    """The manifest's airspace source once openAIP groups have been written."""
    previous = str(previous or "").strip()
    if not previous or "openAIP" in previous or previous.lower().startswith(("none", "skipped", "unavailable")):
        return OPENAIP_AIRSPACE
    return "%s, and %s" % (previous, OPENAIP_AIRSPACE)


def main(argv=None) -> int:
    args = parse_args(argv)
    log = print
    source = Path(args.zip)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(source) as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    # Only the combined navdata holds everything. A world or region ZIP put
    # through here would come out as that subset under the combined name.
    seed_region = str(manifest.get("region") or REGION).upper()
    if seed_region != REGION:
        raise SystemExit("%s is a %s package, not the combined %s navdata."
                         % (source.name, seed_region, REGION))
    airac_display = args.airac or str(manifest.get("airac") or "")
    airac_code = str(manifest.get("airac_code") or "")
    # The code names the staging folder, so it has to be a cycle and nothing else.
    if not airac_code.isdigit() or len(airac_code) != 4:
        raise SystemExit("The manifest's airac_code is not a cycle: %r" % airac_code)

    # Both sources load before anything is unpacked or written. One that
    # fails stops the run: a package without the base would look complete.
    try:
        ourairports = base.load_ourairports(Path(args.cache), log=log)
        openaip = base.load_openaip(Path(args.cache), log=log)
        base_data = base.require_base(openaip, ourairports)
    except (base.BaseDataError, *base.DOWNLOAD_ERRORS) as error:
        print("FAILED: the base data could not be loaded (%s). Nothing was repackaged."
              % error, file=sys.stderr)
        return 1
    if args.airspace_source == "openaip" and not base_data.get("airspaces"):
        print("FAILED: openAIP returned no airspace. Nothing was repackaged.", file=sys.stderr)
        return 1

    stage = out_dir / (".staging-repackage-%s" % airac_code)
    if stage.exists():
        shutil.rmtree(stage)
    with zipfile.ZipFile(source) as archive:
        pack.safe_extract(archive, stage)
    log("Repackaging %s (AIRAC %s) ..." % (source.name, airac_display or airac_code))

    # The published ZIP is already merged. Its base rows come out before the
    # merge runs again, so they are not taken for SkyHigh data and openAIP
    # still beats OurAirports.
    merge.strip_base_rows(stage, manifest, region=REGION, log=log)
    counts = dict(manifest.get("counts") or {})
    counts.update(ourairports.get("counts", {}))
    counts.update(openaip.get("counts", {}))
    counts.update(merge.merge_base_into_stage(stage, base_data, log=log))

    # Older manifests named the overrides folder by its path on the build
    # machine. Such an entry is not carried into the new packages.
    sources = {key: value for key, value in (manifest.get("sources") or {}).items()
               if not pack.local_paths_in({key: value})}
    sources.update(base.SOURCES)

    # The published airspace stays. Only the openAIP groups are rewritten, and
    # only when openAIP was asked for. Otherwise the regions get the drawings
    # the published ZIP has, copied as they are, never redrawn from today's
    # exports.
    bg_dir = stage / "CONFIG" / "bg"
    region_airspaces = None
    if args.airspace_source == "openaip":
        counts["airspace_drawings"] = regions.write_openaip_airspace(
            bg_dir, base_data, "world", airac_display or airac_code, log=log)
        sources["airspace"] = airspace_label(sources.get("airspace"))
        region_airspaces = base_data["airspaces"]

    world = regions.build_global_zip(
        stage, out_dir, airac_display, airac_code, log=log, sources=sources, counts=counts)
    log("Built %s" % world.name)

    region_spec = (args.regions or "").strip().lower()
    if region_spec != "none":
        for prefix in regions.resolve_regions(args.regions, stage, args.min_region_airports):
            regions.build_region_zip(
                prefix, stage, out_dir, airac_display, airac_code,
                base_airspaces=region_airspaces, log=log, sources=sources,
                staged_airspace=region_airspaces is None)

    shutil.rmtree(stage, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
