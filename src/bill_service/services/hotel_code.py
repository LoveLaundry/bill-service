"""Hotel code derivation and bill-number prefix helpers.

Hotel codes are short, uppercase identifiers derived from the client name.
They are used to scope bill-number uniqueness: the same number (e.g. ``1001``)
may exist for multiple hotels, and is stored as ``{CODE}-{number}``
(e.g. ``RH-1001``).

Derivation algorithm
--------------------
Extract the first uppercase letter from each "word" in the hotel name.
If that produces fewer than 2 characters, fall back to the first 3
upper-cased non-space characters of the raw name.

Examples
~~~~~~~~
  "Ramada Hotel"       → RH
  "Cinnamon Grand"     → CG
  "The Kingsbury"      → TK
  "Hilton Colombo"     → HC
  "Jetwing Blue"       → JB
  "Hotel J"            → HJ
  "XYZ"                → XYZ  (single word → first 3 chars)
"""
from __future__ import annotations

import re
from typing import Optional


# Maximum characters in an auto-derived hotel code.
_MAX_CODE_LEN = 5

# Pattern that matches a stored bill number with a hotel-code prefix:
#   {2-5 UPPERCASE LETTERS}-{rest}
_PREFIX_PATTERN = re.compile(r"^([A-Z]{2,5})-(.+)$")


def derive_hotel_code(client_name: str) -> str:
    """Return a short uppercase code for *client_name*.

    Guaranteed to return at least 1 character; typically 2–3.
    """
    name = (client_name or "").strip()
    if not name:
        return "UNK"

    # Strategy 1: initials from each word (words defined as sequences of
    # alphanumeric chars).
    words = re.findall(r"[A-Za-z0-9]+", name)
    initials = "".join(w[0].upper() for w in words if w)
    if len(initials) >= 2:
        return initials[:_MAX_CODE_LEN]

    # Strategy 2: first N uppercase-or-digit characters of the raw name.
    raw = re.sub(r"[^A-Za-z0-9]", "", name).upper()
    return raw[:_MAX_CODE_LEN] or "UNK"


def prefix_bill_number(client_name: str, raw_bill_number: str) -> str:
    """Attach the hotel code prefix to *raw_bill_number* (idempotent).

    If the number is already prefixed (matches the hotel code), it is
    returned unchanged.  This prevents double-prefixing on retries.
    """
    raw = (raw_bill_number or "").strip()
    if not raw:
        return raw

    code = derive_hotel_code(client_name)
    expected_prefix = f"{code}-"

    # Already has this hotel's prefix → idempotent
    if raw.startswith(expected_prefix):
        return raw

    # Has some OTHER prefix (e.g. stored value being re-prefixed) → return as-is.
    # This guards against a double-prefix if the value was already persisted.
    if _PREFIX_PATTERN.match(raw):
        return raw

    return f"{code}-{raw}"


def format_bill_number(stored: Optional[str], include_prefix: bool) -> Optional[str]:
    """Format a stored bill number for display or printing.

    Parameters
    ----------
    stored:
        The value as stored in the DB (may or may not have a prefix).
    include_prefix:
        * ``True``  → return the stored value unchanged.
        * ``False`` → strip the ``{CODE}-`` prefix if present.

    Returns ``None`` when *stored* is ``None``.
    """
    if stored is None:
        return None
    if include_prefix:
        return stored
    m = _PREFIX_PATTERN.match(stored)
    return m.group(2) if m else stored


def strip_prefix(stored: Optional[str]) -> Optional[str]:
    """Convenience wrapper: always strip the prefix (display mode)."""
    return format_bill_number(stored, include_prefix=False)
