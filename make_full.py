#!/usr/bin/env python3
"""Build the full AIRAC as ONE ZIP and publish it.

    python make_full.py

Produces a single ``<cycle>.zip`` asset (e.g. ``2611.zip``) containing
everything — the NATS eAIP SkyHigh data, the global openAIP + OurAirports
base merged underneath, and the openAIP airspace drawings — published to the
``airac-<cycle>`` GitHub release as the only asset. Every consumer (skymaps,
SkyRadar, anything else) pulls from that one file.

Any ``make_airac.py`` flag passes through:

    python make_full.py --no-publish        # build only
    python make_full.py --skip-layouts      # faster local test build
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from make_airac import main

if __name__ == "__main__":
    if "--single" not in sys.argv:
        sys.argv.insert(1, "--single")
    sys.exit(main())
