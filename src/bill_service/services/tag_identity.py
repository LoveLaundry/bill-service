"""Structured linen tag identities.

A structured tag is ``{ITEM}{HOTEL}-{SERIES}{NUMBER}{CHECK}`` — for example
``BSG-A0001XY``:

* ``BS`` — bedsheet, derived from the selected linen category/item type;
* ``G`` — the owning hotel's code, read from the hotel registry record;
* ``A0001`` — the sequence: a series letter plus four digits, seeded by the
  number the admin chooses (1 → A0001, 10000 → B0001, …);
* ``XY`` — two uppercase check letters derived from the rest of the ID, used to
  catch mistyped scans.

The hotel code is never guessed from the hotel name here. The admin selects a
registry record in the UI and the backend stores the code supplied by that
record alongside the generated tag.
"""

from __future__ import annotations

import re

# Ordered so the more specific phrase wins (``bath mat`` before ``mat``, etc.).
_ITEM_CODE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("bath mat", "BM"),
    ("bathmat", "BM"),
    ("pillow case", "PC"),
    ("pillowcase", "PC"),
    ("duvet cover", "DC"),
    ("duvet", "DC"),
    ("table cloth", "TC"),
    ("tablecloth", "TC"),
    ("bed sheet", "BS"),
    ("bedsheet", "BS"),
    ("sheet", "BS"),
    ("bath towel", "TW"),
    ("towel", "TW"),
    ("napkin", "NK"),
    ("uniform", "UN"),
    ("robe", "RB"),
    ("slipper", "SL"),
    ("curtain", "CR"),
    ("blanket", "BL"),
)

_FALLBACK_ITEM_CODES: dict[str, str] = {
    "BEDSHEET": "BS",
    "PILLOWCASE": "PC",
    "TOWEL": "TW",
    "DUVET_COVER": "DC",
    "BATH_MAT": "BM",
    "UNIFORM": "UN",
    "TABLECLOTH": "TC",
    "NAPKIN": "NK",
    "ROBE": "RB",
    "SLIPPER": "SL",
    "OTHER": "OT",
}

_OWNER_CODE = re.compile(r"^[A-Z0-9]{1,5}$")
# Same contract in both stacks: two letters, hotel, series letter, four digits,
# two check letters. 0000 is not a valid number.
_STRUCTURED_ID = re.compile(
    r"^([A-Z]{2}[A-Z0-9]{1,5})-([A-Z])(\d{4})([A-Z]{2})$"
)

# 9999 numbers per series letter, never 0000 (which reads as a blank field).
_NUMBERS_PER_SERIES = 9999
MAX_LINEN_SEQUENCE = 26 * _NUMBERS_PER_SERIES


def item_code(category: str, item_type: str = "") -> str:
    """Derive the two-letter item part of a structured tag."""

    normalized = " ".join(
        " ".join(re.sub(r"[^a-z0-9]+\s*", " ", str(value or "").lower()).split())
        for value in (category, item_type)
    ).strip()
    for keyword, code in _ITEM_CODE_KEYWORDS:
        if keyword in normalized:
            return code

    for value in (category, item_type):
        key = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
        if key in _FALLBACK_ITEM_CODES:
            return _FALLBACK_ITEM_CODES[key]
        if len(key) >= 2:
            return key[:2]
    return "OT"


def owner_code(value: str) -> str:
    """Validate and normalize the hotel code copied from a registry record."""

    code = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
    if not _OWNER_CODE.match(code):
        raise ValueError("The selected hotel needs a 1-5 character hotel code")
    return code


def series_and_number(sequence: int) -> tuple[str, int]:
    """Map a 1-based sequence to ``('A', 1)`` … ``('Z', 9999)``."""

    value = int(sequence)
    if value < 1 or value > MAX_LINEN_SEQUENCE:
        raise ValueError(
            f"Linen sequence must be between 1 and {MAX_LINEN_SEQUENCE}"
        )
    offset = value - 1
    return chr(65 + offset // _NUMBERS_PER_SERIES), offset % _NUMBERS_PER_SERIES + 1


def checksum(base: str) -> str:
    """Two check letters for ``ITEM-HOTELSERIESNUMBER`` (mod 676).

    Weighted by character position so a swapped pair of characters cannot pass.
    The algorithm is deliberately dependency-free so scanners in every stack
    can recompute it offline.
    """

    text = base.replace("-", "").upper()
    total = sum((index + 1) * ord(char) for index, char in enumerate(text))
    value = total % (26 * 26)
    return chr(65 + value // 26) + chr(65 + value % 26)


def structured_linen_id(
    category: str,
    item_type: str,
    hotel_code: str,
    sequence: int,
) -> str:
    """Return a permanent ID such as ``BSG-A0001XY``."""

    series, number = series_and_number(sequence)
    base = f"{item_code(category, item_type)}{owner_code(hotel_code)}-{series}{number:04d}"
    return base + checksum(base)


def is_valid_structured_id(value: str) -> bool:
    """True only for a well-formed tag whose check letters match."""

    match = _STRUCTURED_ID.match(str(value or "").strip().upper())
    if not match:
        return False
    prefix, series, digits, check = match.groups()
    if not 1 <= int(digits) <= _NUMBERS_PER_SERIES:
        return False
    base = f"{prefix}-{series}{digits}"
    return check == checksum(base)
