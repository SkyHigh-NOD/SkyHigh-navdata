"""Offline validation: extract the published EG_2610.zip into a staging tree,
run the base merge + per-FIR packaging on it, and sanity-check the outputs."""
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skyhigh_navdata import base, firs, merge

STAGE = Path("dist/.test-staging")
CACHE = Path(".cache")

with zipfile.ZipFile("dist/EG_2610-src.zip") as archive:
    archive.extractall(STAGE)

log = print
ourairports = base.load_ourairports(CACHE, log=log)
openaip = base.load_openaip(CACHE, log=log)
base_data = dict(openaip)
base_data["ourairports"] = ourairports
counts = merge.merge_base_into_stage(STAGE, base_data, log=log)

# replace the broken eAIP airspace with openAIP for the combined scope
for stale in (STAGE / "CONFIG" / "bg").glob("*"):
    if stale.is_dir():
        import shutil
        shutil.rmtree(stale)
    else:
        stale.unlink()
added, _group = firs.write_openaip_airspace_group(
    STAGE / "CONFIG" / "bg", base_data.get("airspaces", []),
    firs.EG_REGION["bbox"], "10/26",
    countries=firs.EG_REGION["countries"])
print("combined openAIP airspace drawings:", added)

for fix_file in ("fix.dat", "navaid.dat", "AD-positions"):
    text = (STAGE / "CONFIG" / "nav" / fix_file).read_text(encoding="utf-8", errors="replace")
    print("%s: %d lines" % (fix_file, len(text.splitlines())))

for fir_id in ("EGTT", "EGPX", "EGGX", "EISN"):
    firs.build_fir_zip(
        fir_id, firs.FIRS[fir_id], STAGE, Path("dist"),
        "10/26", "2610",
        base_airspaces=base_data.get("airspaces"),
        generator_version="1.0.0",
        log=log,
    )
print("OK")
