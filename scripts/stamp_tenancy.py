#!/usr/bin/env python3
"""One-time, idempotent backfill of ``laundry_id`` on pre-tenancy bill documents.

Tenant reads treat a document that lacks ``laundry_id`` as belonging to
``lovelaundry`` — the only tenant that existed before multi-tenancy (see
``bill_service.tenancy``). Running this script materialises that assumption so
tenant-scoped indexes, counts and exports are exact.

Only documents MISSING ``laundry_id`` are ever touched; an existing value is
never overwritten, so the script is safe to run repeatedly and against a live
database. Nothing is written unless ``--apply`` is given, which makes the
default a read-only report (``--dry-run`` is an explicit synonym).

Run from the ``bill-service`` directory::

    python -m scripts.stamp_tenancy                       # report only
    python -m scripts.stamp_tenancy --apply               # persist backfill
    python -m scripts.stamp_tenancy --apply --laundry-id lovelaundry
    python -m scripts.stamp_tenancy --only bills,payments --apply
    python -m scripts.stamp_tenancy --skip audit_logs --apply

Collections outside :data:`TENANT_COLLECTIONS` are never selected: numbering
sequences, sync ledgers and idempotency keys have no tenant dimension, and
stamping them would be a silent write to infrastructure.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.bill_service.database.connection_manager import ROLE_MAIN, get_database
from src.bill_service.tenancy import LOVE_LAUNDRY_ID, stamp_missing_laundry_ids

MISSING_LAUNDRY_ID = {"$or": [{"laundry_id": {"$exists": False}}, {"laundry_id": None}]}

# Every collection the tenancy contract governs: reads go through
# scoped_query/owned_filter/match_stage, writes stamp laundry_id.
TENANT_COLLECTIONS: Sequence[str] = (
    "bills",
    "gatepasses",
    "deliveries",
    "dispatch_jobs",
    "payments",
    "loyalty_accounts",
    "audit_logs",
    "linens",
    "linen_events",
    "returns",
    "adjustments",
    "shop_bills",
    "bill_templates",
    "legacy_invoices",
    "rewashes",
    "monthly_entries",
    "linen_category_mappings",
)


def select_collections(
    available: Iterable[str],
    only: Optional[Set[str]] = None,
    skip: Optional[Set[str]] = None,
) -> List[str]:
    """Return the tenant collections present in ``available`` to stamp.

    ``only`` acts as an exclusive allow-list, ``skip`` removes entries;
    neither can widen the set beyond :data:`TENANT_COLLECTIONS`.
    """
    candidates = set(TENANT_COLLECTIONS) & set(available)
    if only:
        candidates &= only
    return sorted(candidates - set(skip or ()))


async def stamp_database(
    db: Any,
    laundry_id: str = LOVE_LAUNDRY_ID,
    *,
    apply: bool = False,
    only: Optional[Set[str]] = None,
    skip: Optional[Set[str]] = None,
) -> Dict[str, int]:
    """Stamp every selected collection, returning ``{collection: count}``.

    With ``apply=False`` counts come from ``count_documents`` and nothing is
    written.
    """
    names = await db.list_collection_names()
    counts: Dict[str, int] = {}
    for name in select_collections(names, only, skip):
        collection = db[name]
        if apply:
            counts[name] = (await stamp_missing_laundry_ids({name: collection}, laundry_id))[name]
        else:
            counts[name] = await collection.count_documents(MISSING_LAUNDRY_ID)
    return counts


def _report(counts: Dict[str, int], laundry_id: str, applied: bool) -> None:
    verb = "stamped" if applied else "would stamp"
    for name in sorted(counts):
        print(f"  {counts[name]:>8}  {name}")
    print(f"  {'-' * 8}")
    print(f"  {sum(counts.values()):>8}  TOTAL ({verb}, laundry_id={laundry_id})")


def _split(value: str) -> Optional[Set[str]]:
    items = {part.strip() for part in value.split(",") if part.strip()}
    return items or None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--laundry-id", default=LOVE_LAUNDRY_ID, help="tenant id to write (default: %(default)s)"
    )
    parser.add_argument("--apply", action="store_true", help="persist changes (default: report only)")
    parser.add_argument("--dry-run", action="store_true", help="report only (the default)")
    parser.add_argument("--only", default="", help="comma-separated allow-list of collections")
    parser.add_argument("--skip", default="", help="comma-separated collections to skip")
    args = parser.parse_args(argv)

    apply = args.apply and not args.dry_run

    async def _run() -> Dict[str, int]:
        return await stamp_database(
            get_database(ROLE_MAIN),
            args.laundry_id,
            apply=apply,
            only=_split(args.only),
            skip=_split(args.skip),
        )

    try:
        counts = asyncio.run(_run())
    except Exception as exc:  # pragma: no cover - operator-facing
        print(f"stamp_tenancy failed: {exc}", file=sys.stderr)
        return 1

    _report(counts, args.laundry_id, apply)
    if not apply:
        print("(report only – re-run with --apply to persist)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
