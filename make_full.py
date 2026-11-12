#!/usr/bin/env python3
"""Build the full AIRAC as ONE ZIP and publish it — with a menu.

    python make_full.py

Shows a menu at the start: a full worldwide build (everything in
``<cycle>.zip``), single-part regeneration (just airspace, just fixes, ...),
or picking several parts at once. Any ``make_airac.py`` flag still passes
through for non-interactive use:

    python make_full.py --no-publish        # build only, no menu prompt for it
    python make_full.py --only airways      # skips the menu
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

MENU = """
SkyHigh navdata builder - AIRAC {cycle_label}
------------------------------------------------------------
 1) Full worldwide fresh AIRAC + publish
      one <cycle>.zip: worldwide airspace, aerodromes, fixes, navaids,
      airways, entry/exit points + worldwide aerodrome layouts
      (layouts hit the X-Plane Gateway for 40k+ aerodromes - allow hours)
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


def _ask_cycle_label(code_hint: str) -> str:
    return code_hint or "(latest release)"


def _prompt_publish(default_yes: bool = True) -> list:
    answer = input("Publish to GitHub when done? [Y/n]: ").strip().lower()
    if answer in ("n", "no"):
        return ["--no-publish"]
    if default_yes and answer in ("", "y", "yes"):
        return []
    return ["--no-publish"] if answer in ("n", "no") else []


def _multi_pick() -> list:
    items = ("airspace fixes ad-positions ctr layouts eaip-bg").split()
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
    if "airspace" in chosen:
        args += ["--airspace-scope", "world"]
    if "layouts" in chosen:
        spec = input("Layout scope - w)orld or comma-separated ICAO region prefixes "
                     "(e.g. EG,LF) [w]: ").strip()
        args += ["--layouts", spec or "world"]
    return args


def main_menu() -> list:
    """Returns the argv tail for make_airac.main()."""
    while True:
        print(MENU.format(cycle_label=""))
        choice = input("Choice: ").strip().lower()
        if choice == "1":
            print("NOTE: worldwide layouts take hours on the first run "
                  "(cached afterwards only for the eAIP, not the Gateway).")
            publish = _prompt_publish()
            return ["--single", "--airspace-scope", "world", "--layouts", "world", *publish]
        if choice == "2":
            publish = _prompt_publish()
            return ["--only", "airspace", "--airspace-scope", "world", *publish]
        if choice in ("3", "4", "7"):
            part = {"3": "fixes", "4": "ad-positions", "7": "ctr"}[choice]
            publish = _prompt_publish()
            return ["--only", part, *publish]
        if choice == "5":
            print("NOTE: worldwide layouts take hours on the first run "
                  "(cached afterwards only for the eAIP, not the Gateway).")
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


if __name__ == "__main__":
    # any argument = non-interactive: skip the menu entirely
    if len(sys.argv) > 1:
        if "--single" not in sys.argv:
            sys.argv.insert(1, "--single")
    else:
        sys.argv = [sys.argv[0], *main_menu()]
    from make_airac import main
    sys.exit(main())
