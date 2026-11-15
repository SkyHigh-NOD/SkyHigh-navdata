# SkyHigh-navdata

Builds the navigation data SkyRadar loads, packaged as a ZIP **release asset**
named after the FIR and the AIRAC cycle, e.g. **`EG_2610.zip`** (cycle 10/26).

SkyRadar downloads and installs this ZIP from the repository's latest release.

## What it does

1. Downloads the current **NATS UK AIRAC** archive (eAIP HTML).
2. Parses it into SkyRadar's `CONFIG/nav` files:
   - `navaid.dat` (ENR 4.1)
   - `fix.dat` (ENR 4.4)
   - `awy.dat` (ENR 3.2)
   - `AD-positions` (aerodrome reference points, AD 2)
   - `AD-entry-exit` (SID/STAR entry/exit points, AD 2)
   - `CTR-positions` (default enroute radar position for the launcher's CTR
     dropdown, so a fresh install has `ENR21` to start from)
3. Builds the airspace drawings SkyRadar shows in `CONFIG/bg`:
   - `ENR21.lst` + `ENR21/*.draw` (FIR/UIR/TMA/CTA, from ENR 2.1). The
     volumes are grouped by named area, so each `.draw` is one togglable
     "sector" on the radar; `ENR21.sectors` carries each one's owning unit,
     callsign and frequencies.
   - `<ICAO>.lst` + `<ICAO>/*.draw` (local CTR/ATZ/TMZ, from each airport's
     AD 2.17)
4. Downloads the latest **X-Plane Gateway** airport layout for every aerodrome
   into `CONFIG/ad/<ICAO>.dat`.
5. Copies the hand-maintained **`CONFIG/` overrides** (see below) over the
   generated files, so local customisations always win.
6. Writes a `manifest.json` (region, AIRAC cycle, build time, file list with
   sizes and SHA-256 hashes).
7. Zips it all into `dist/EG_<airac>.zip`.
8. Writes matching **release notes** next to it, `dist/EG_<airac>.md`, ready to
   paste into the GitHub release.

The ZIP mirrors SkyRadar's own folder layout, so it can be extracted straight
over a SkyRadar installation.

## Requirements

Python 3.10+ and:

```
pip install -r requirements.txt
```

## Usage

### One command for a whole cycle

```
python make_airac.py                      # build the current cycle into dist/
python make_airac.py --airac 2611         # force a cycle label
python make_airac.py --regions LF,ED,K    # restrict per-region ZIPs
python make_airac.py --publish            # build, then upload to the release
python make_airac.py --publish --clobber  # ... replacing assets already there
```

`make_airac.py` runs the whole pipeline with a live progress bar (overall
percent, elapsed time and ETA): NATS eAIP download → CONFIG/nav parse →
openAIP + OurAirports global base → openAIP airspace → `CONFIG/` overrides →
merge → X-Plane Gateway layouts → combined `EG_<airac>.zip`, world
`ALL_<airac>.zip` for skymaps and one ZIP per ICAO region.

**Nothing is uploaded unless `--publish` is given.** SkyRadar installs
download from the release, so an upload changes what every install gets.
`--publish` uploads to the release tagged `airac-<cycle>` through `gh` (the
GitHub CLI, installed and signed in) and refuses to touch an asset that is
already on the release; `--clobber` replaces it. Publish from a checkout of
`main`, not from a branch under review.

`make_full.py` is a menu in front of the same script. It asks before
uploading and only `y` or `yes` uploads.

### Manual steps

```
python build_navdata.py                           # full build (nav + airspace + layouts)
python build_navdata.py --skip-layouts            # everything except X-Plane layouts
python build_navdata.py --skip-airspace           # everything except CONFIG/bg drawings
python build_navdata.py --airac 2610 --source-dir ./html   # offline from extracted eAIP
python build_navdata.py --airac-zip AIRAC-10-26.zip
```

### Selective regeneration

Don't rebuild everything for one change — regenerate just the part you need
from the existing navdata (staged from the newest combined ZIP in `dist/`,
that is `EG_<airac>.zip` or `<airac>.zip`, or `--from-zip <path>`; a world or
region ZIP is never used as the seed):

```
python make_airac.py --only airways                     # airways only, then repackage
python make_airac.py --only navaids,fixes               # both, plus the base re-merge
python make_airac.py --only layouts                     # re-pull X-Plane layouts
python make_airac.py --only airspace                    # rebuild both sets of airspace drawings
python make_airac.py --only airspace --airspace-source openaip   # just the openAIP groups
python make_airac.py --only download,ad-positions       # fresh eAIP + aerodrome positions
```

Items: `download` (NATS eAIP), `fixes`, `navaids`, `airways`, `ad-positions`,
`entry-exit`, `ctr`, `layouts` (X-Plane Gateway), `airspace` (per
`--airspace-source`). The global base re-merges automatically after any nav
regeneration, and the result is packaged under the seed ZIP's name. It is
uploaded only with `--publish`, and then the release's notes are left as they
are, since they list every asset and only one was replaced.

`--airac` accepts `2610` (YYMM), `10/26`, or `202610`. When omitted it is
detected from NATS. Useful flags: `--out dist`, `--name`, `--work-dir`,
`--skip-layouts`, `--skip-airspace`, `--overrides-dir`, `--no-overrides`,
`--notes-out`, `--no-notes`, `--quiet`. Run `python build_navdata.py -h` for
everything.

## Global base data

The NATS eAIP covers the UK. Two more sources go in underneath it, so a
SkyRadar away from the UK still has aerodromes, navaids and airspace:

| Source | What it adds | License |
|---|---|---|
| [openAIP](https://www.openaip.net) daily exports | airports, navaids, VFR reporting points, airspace | CC BY-NC 4.0 |
| [OurAirports](https://ourairports.com) | aerodrome positions openAIP does not list | public domain |

- `navaid.dat` gets the openAIP navaids the eAIP does not have. Idents repeat
  around the world, so a navaid only counts as already present when the ident
  matches and it is within 10 NM.
- `AD-positions` gets openAIP's aerodromes, then OurAirports'. Only four-letter
  ICAO codes: local identifiers such as `00AK` are left out.
- `fix.dat` is not touched. openAIP's reporting points are VFR points with
  spaces in their names; they go to `CONFIG/nav/vfr-points` (tab-separated
  latitude, longitude, ISO country, name).
- `CONFIG/nav/NOTICE.txt` credits the sources and ships in every ZIP that
  holds base data. The manifest's `sources` names them as well. openAIP's
  license requires the credit and allows non-commercial use only.

`--no-base` leaves all of it out; no world or region ZIPs are built then.

Airspace drawings come from both sources by default, because the two readers
want different ones:

- the eAIP drawings, `CONFIG/bg/ENR21` and the per-aerodrome groups, which the
  skymaps chart build reads;
- the openAIP per-country groups, `CONFIG/bg/OAIP<ISO>.lst` (`OAIPGB`,
  `OAIPFR`, ...) with `CONFIG/bg/OAIP-countries`, which SkyRadar reads to pick
  the group for a position.

`--airspace-source both|eaip|openaip` (default `both`) narrows that, in
`build_navdata.py` and `make_airac.py` alike, and the choice applies to every
ZIP the build writes. `--airspace-scope world|eg` (default `world`) limits the
openAIP groups to the UK and Ireland. The eAIP covers the UK only, so a region
ZIP elsewhere carries openAIP groups or no drawings.

## Tests

```
pip install pytest
python -m pytest
```

## Customised files (local overrides)

Anything kept in this repository's `CONFIG/` folder is copied over the generated
output, so hand-edited sector and layout files always win and are shipped in the
release exactly as written:

```
CONFIG/
└── ad
    └── EGSS.dat      # hand-made Stansted layout; replaces the Gateway one
```

- The folder mirrors the ZIP, so `CONFIG/ad/EGSS.dat` replaces the Gateway-built
  `CONFIG/ad/EGSS.dat`, `CONFIG/bg/EGLL.lst` replaces a generated airspace list,
  `CONFIG/nav/navaid.dat` replaces the NATS-derived navaids, and so on.
- A custom `CONFIG/ad/<ICAO>.dat` also **skips** the X-Plane Gateway download for
  that aerodrome, so the build is faster and works offline for those.
- Only `CONFIG/nav/`, `CONFIG/ad/` and `CONFIG/bg/` are ever shipped; anything
  else under `CONFIG/` is ignored. `airac.txt` and `manifest.json` are always
  generated and are never overridden.
- Editing a file here and re-running the build is all that is needed — the
  override is picked up automatically.
- Overrides are copied in before the base merge, in both `build_navdata.py`
  and `make_airac.py`, so an overridden `navaid.dat` or `AD-positions` still
  gets the base rows it does not have.
- Disable with `--no-overrides`, or read them from elsewhere with
  `--overrides-dir DIR`. The build summary reports `overrides=N`.

## Publishing a release

For each new AIRAC cycle:

1. On a checkout of `main`, run `python build_navdata.py` → `dist/EG_2610.zip`
   (cycle 10/26) plus `dist/EG_2610.md` (the release notes).
2. Create a GitHub release; tag it with the cycle, e.g. `airac-2610`.
3. Upload the ZIP as a release asset and paste `dist/EG_2610.md` into the
   release description.

`python make_airac.py --publish` does steps 2 and 3 with `gh`.

The asset name **includes the cycle** (`EG_2610.zip`), so the name changes every
cycle — that is intentional. SkyRadar does not use a fixed URL: it queries
`https://api.github.com/repos/SkyHigh-NOD/SkyHigh-navdata/releases/latest` and
installs the asset matching `<FIR>_<airac>.zip`, falling back to a single
`<airac>.zip` (what `make_airac.py --single` builds). Because of that you only need to
mark each release as **latest**, and multiple FIRs can share one release.

## Layout of the ZIP

```
EG_2610.zip
├── manifest.json
└── CONFIG
    ├── ad
    │   ├── EGLL.dat
    │   └── ...
    ├── bg
    │   ├── ENR21.lst
    │   ├── ENR21.sectors
    │   ├── ENR21
    │   │   └── *.draw
    │   ├── EGLL.lst
    │   └── EGLL
    │       └── *.draw
    └── nav
        ├── AD-entry-exit
        ├── AD-positions
        ├── CTR-positions
        ├── NOTICE.txt        (with base data)
        ├── airac.txt
        ├── awy.dat
        ├── fix.dat
        ├── navaid.dat
        └── vfr-points        (with base data)
```

SkyRadar refuses to install any file outside `CONFIG/nav/`, `CONFIG/ad/` or
`CONFIG/bg/`, and verifies each file's SHA-256 against the manifest before
writing it. Files it installs are recorded in `CONFIG/nav/.airac-status.json` so
a later release can prune anything that is no longer published; existing files
that are not part of the release (for example `CONFIG/bg/Notice` or
`CONFIG/nav/route-presets`) are never touched.
