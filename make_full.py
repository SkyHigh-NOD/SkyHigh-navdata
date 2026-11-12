#!/usr/bin/env python3
"""Build the full AIRAC as ONE ZIP and publish it — with a menu.

    python make_full.py

Shows a menu at the start: a full worldwide build (everything in
``<cycle>.zip``), single-part regeneration (just airways, just navaids, ...),
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
 1) FULL worldwide build + publish
      one <cycle>.zip: worldwide airspace, aerodromes, fixes, navaids,
      airways, entry/exit points + worldwide aerodrome layouts
      (layouts hit the X-Plane Gateway for 40k+ aerodromes - allow hours)
 2) Full build + publish, EG layouts only (much faster)
 3) Just airways
 4) Just navaids
 5) Just fixes
 6) Just aerodrome positions (AD-positions)
 7) Just entry/exit points (SIDs/STARs)
 8) Just CTR positions
 9) Just aerodrome layouts (e)g, w)orld, or add region prefixes e.g. EG,LF)
10) Just airspace (worldwide openAIP drawings)
11) Just eAIP bg drawings (ENR21 + per-aerodrome CTR/ATZ)
 m) Pick multiple parts to regenerate
 p) Change publish behaviour is on the flags; q) Quit
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
    items = ("download fixes navaids airways ad-positions entry-exit ctr "
             "layouts airspace eaip-bg").split()
    print("Available parts: %s" % ", ".join(items))
    raw = input("Parts to regenerate (comma-separated, blank = cancel): ").strip().lower()
    chosen = [p.strip() for p in raw.replace(" ", ",").split(",") if p.strip()]
    unknown = [p for p in chosen if p not in items]
    if unknown:
        print("Unknown part(s): %s" % ", ".join(unknown))
        return []
    if not chosen:
        return []
    return ["--only", ",".join(chosen)]


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
            return ["--single", "--airspace-scope", "world", *publish]
        if choice in ("3", "4", "5", "6", "7", "8", "9", "10", "11"):
            part = {
                "3": "airways", "4": "navaids", "5": "fixes",
                "6": "ad-positions", "7": "entry-exit", "8": "ctr",
                "9": "layouts", "10": "airspace", "11": "eaip-bg",
            }[choice]
            scope_args = []
            if part == "layouts":
                spec = input(
                    "Scope - e)g, w)orld, or comma-separated ICAO region prefixes "
                    "to add/refresh (e.g. EG,LF): ").strip()
                scope_args = ["--layouts", spec or "eg"]
            elif part == "airspace":
                scope = input("Scope - e)g only or w)orld [e]: ").strip().lower()
                scope_args = ["--airspace-scope",
                              "world" if scope.startswith("w") else "eg"]
            publish = _prompt_publish()
            return ["--only", part, *scope_args, *publish]
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
