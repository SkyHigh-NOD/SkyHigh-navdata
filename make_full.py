#!/usr/bin/env python3
"""Menu front end for make_airac.py.

    python make_full.py

Shows a menu: a full worldwide build (everything in ``<cycle>.zip``),
single-part regeneration (just airspace, just fixes, ...), or several parts at
once. Each choice asks whether to upload the result; only a plain "y" or "yes"
does. With any argument the menu is skipped and the arguments go to
``make_airac.py`` exactly as given:

    python make_full.py --only airways      # same as: python make_airac.py --only airways
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

MENU = """
SkyHigh navdata builder
------------------------------------------------------------
 1) Full worldwide fresh AIRAC
      one <cycle>.zip: worldwide airspace, aerodromes, navaids,
      airways, entry/exit points + worldwide aerodrome layouts
      (layouts hit the X-Plane Gateway for every aerodrome - allow hours)
 2) Just airspace (skymaps + skyradar)
 3) Just fixes
 4) Just AD positions
 5) Just AD layouts (global - allow hours)
 6) Add AD layout region to an existing set
 7) Just CTR positions
 m) Multiple parts
 q) Quit
------------------------------------------------------------
"""


def publish_args(answer: str) -> list:
    """Arguments for the answer to "upload?": only a clear yes publishes.

    An empty answer, a typo or anything else builds without uploading. The
    upload replaces what SkyRadar installs download, so it is never the
    default.
    """
    return ["--publish"] if answer.strip().lower() in ("y", "yes") else []


def _prompt_publish() -> list:
    chosen = publish_args(input("Upload to the GitHub release when done? [y/N]: "))
    if not chosen:
        print("Building only. Nothing will be uploaded.")
        return chosen
    replace = input("Replace assets already on the release? [y/N]: ")
    return chosen + (["--clobber"] if replace.strip().lower() in ("y", "yes") else [])


def _multi_pick() -> list:
    items = ("airspace fixes ad-positions ctr layouts").split()
    print("Available parts: %s" % ", ".join(items))
    raw = input("Parts to regenerate (comma-separated, blank = cancel): ").strip().lower()
    chosen = [p.strip() for p in raw.replace(" ", ",").split(",") if p.strip()]
    unknown = [p for p in chosen if p not in items]
    if unknown:
        print("Unknown part(s): %s" % ", ".join(unknown))
        return []
    if not chosen:
        return []
    args = ["--only", ",".join(chosen)]
    if "layouts" in chosen:
        spec = input("Layout scope - w)orld or comma-separated ICAO region prefixes "
                     "(e.g. EG,LF) [w]: ").strip()
        args += ["--layouts", "world" if spec.lower() in ("", "w", "world") else spec]
    return args


def main_menu() -> list:
    """Returns the argv tail for make_airac.main()."""
    while True:
        print(MENU)
        choice = input("Choice: ").strip().lower()
        if choice == "1":
            print("NOTE: worldwide layouts take hours.")
            publish = _prompt_publish()
            return ["--single", "--layouts", "world", *publish]
        if choice == "2":
            # the eAIP drawings skymaps reads, plus the per-country openAIP
            # groups and the OAIP-countries map SkyRadar reads
            publish = _prompt_publish()
            return ["--only", "airspace", *publish]
        if choice in ("3", "4", "7"):
            part = {"3": "fixes", "4": "ad-positions", "7": "ctr"}[choice]
            publish = _prompt_publish()
            return ["--only", part, *publish]
        if choice == "5":
            print("NOTE: worldwide layouts take hours.")
            publish = _prompt_publish()
            return ["--only", "layouts", "--layouts", "world", *publish]
        if choice == "6":
            spec = input("Region prefixes to add/refresh (comma-separated, "
                         "e.g. EG,LF): ").strip()
            if not spec:
                print("No regions given.\n")
                continue
            publish = _prompt_publish()
            return ["--only", "layouts", "--layouts", spec, *publish]
        if choice == "m":
            picked = _multi_pick()
            if picked:
                publish = _prompt_publish()
                return [*picked, *publish]
            continue
        if choice in ("q", "quit", "exit"):
            raise SystemExit(0)
        print("Pick a number from the menu.\n")


def resolve_argv(argv: list) -> list:
    """The make_airac.py arguments for this run.

    Arguments given on the command line pass through untouched: nothing is
    added to them, so the asset keeps the name make_airac.py gives it
    (``EG_<cycle>.zip`` unless ``--single`` was asked for). With none, the
    menu decides.
    """
    return list(argv) if argv else main_menu()


if __name__ == "__main__":
    from make_airac import main
    sys.exit(main(resolve_argv(sys.argv[1:])))
