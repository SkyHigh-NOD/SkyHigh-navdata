"""AIRAC cycle identifiers.

Two forms are used throughout:

* **code** — four digits ``YYMM``, e.g. ``2610``. Used in release asset and file
  names (``EG_2610.zip``).
* **display** — ``MM/YY``, e.g. ``10/26``. The human-readable form shown in
  SkyRadar and stored in ``manifest.json`` / ``.airac-status.json``.
"""
from __future__ import annotations

import re

_CODE_RE = re.compile(r"^(\d{2})(\d{2})$")
_DISPLAY_RE = re.compile(r"^(\d{1,2})\s*[/-]\s*(\d{2}|\d{4})$")
_YEAR_FIRST_RE = re.compile(r"^(\d{4})\s*-\s*(\d{1,2})$")


class CycleError(ValueError):
    """Raised when an AIRAC cycle cannot be understood."""


def _code_from_parts(year: str, month: str) -> str:
    month_int = int(month)
    if not 1 <= month_int <= 12:
        raise CycleError("AIRAC month out of range: %s" % month)
    return "%02d%02d" % (int(year) % 100, month_int)


def to_code(value: str) -> str:
    """Normalise any accepted AIRAC form to the four-digit ``YYMM`` code.

    Accepts ``2610``, ``10/26``, ``202610`` and ``2026-10``.
    """
    if value is None:
        raise CycleError("Missing AIRAC cycle")
    text = str(value).strip()
    if not text:
        raise CycleError("Missing AIRAC cycle")

    if text.isdigit() and len(text) == 4:
        # Already a code; still validate the month part (YYMM).
        _code_from_parts(text[0:2], text[2:4])
        return text
    if text.isdigit() and len(text) == 6:  # YYYYMM
        return _code_from_parts(text[2:4], text[4:6])

    display = _DISPLAY_RE.match(text)
    if display:
        month, year = display.group(1), display.group(2)
        return _code_from_parts(year, month)

    year_first = _YEAR_FIRST_RE.match(text)
    if year_first:
        return _code_from_parts(year_first.group(1), year_first.group(2))

    raise CycleError("Unrecognised AIRAC cycle: %r (expected e.g. 2610 or 10/26)" % value)


def to_display(code: str) -> str:
    """``2610`` -> ``10/26``."""
    code = to_code(code)
    return "%s/%s" % (code[2:4], code[0:2])


def normalize(value: str):
    """Return ``(code, display)``."""
    code = to_code(value)
    return code, to_display(code)
