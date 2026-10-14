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

```
python build_navdata.py                           # full build (nav + airspace + layouts)
python build_navdata.py --skip-layouts            # everything except X-Plane layouts
python build_navdata.py --skip-airspace           # everything except CONFIG/bg drawings
python build_navdata.py --airac 2610 --source-dir ./html   # offline from extracted eAIP
python build_navdata.py --airac-zip AIRAC-10-26.zip
```

`--airac` accepts `2610` (YYMM), `10/26`, or `202610`. When omitted it is
detected from NATS. Useful flags: `--out dist`, `--name`, `--work-dir`,
`--skip-layouts`, `--skip-airspace`, `--overrides-dir`, `--no-overrides`,
`--notes-out`, `--no-notes`, `--quiet`. Run `python build_navdata.py -h` for
everything.

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
- Disable with `--no-overrides`, or read them from elsewhere with
  `--overrides-dir DIR`. The build summary reports `overrides=N`.

## Publishing a release

For each new AIRAC cycle:

1. Run `python build_navdata.py` → `dist/EG_2610.zip` (cycle 10/26) plus
   `dist/EG_2610.md` (the release notes).
2. Create a GitHub release; tag it with the cycle, e.g. `airac-2610`.
3. Upload the ZIP as a release asset and paste `dist/EG_2610.md` into the
   release description.

The asset name **includes the cycle** (`EG_2610.zip`), so the name changes every
cycle — that is intentional. SkyRadar does not use a fixed URL: it queries
`https://api.github.com/repos/SkyHigh-NOD/SkyHigh-navdata/releases/latest` and
installs the asset matching `<FIR>_<airac>.zip`. Because of that you only need to
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
        ├── airac.txt
        ├── awy.dat
        ├── fix.dat
        └── navaid.dat
```

SkyRadar refuses to install any file outside `CONFIG/nav/`, `CONFIG/ad/` or
`CONFIG/bg/`, and verifies each file's SHA-256 against the manifest before
writing it. Files it installs are recorded in `CONFIG/nav/.airac-status.json` so
a later release can prune anything that is no longer published; existing files
that are not part of the release (for example `CONFIG/bg/Notice` or
`CONFIG/nav/route-presets`) are never touched.
