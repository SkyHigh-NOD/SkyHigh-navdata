"""SkyHigh navdata builder.

Standalone tool that turns NATS UK eAIP sources and X-Plane Gateway airport
layouts into the ``CONFIG/...`` folder structure SkyRadar reads, packaged as a
ZIP that is published as a GitHub release asset and downloaded by SkyRadar.
"""

__version__ = "1.0.0"

GENERATOR_NAME = "skyhigh-navdata"
REGION = "EG"
MANIFEST_NAME = "manifest.json"
