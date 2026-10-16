#!/usr/bin/env python3
"""One-time, idempotent backfill of ``laundry_id`` on pre-tenancy documents.

Tenant reads treat a document that lacks ``laundry_id`` as belonging to
``lovelaundry`` — the only tenant that existed before multi-tenancy. Running
this script materialises that assumption so tenant-scoped indexes, counts and
exports are exact.

Only documents MISSING ``laundry_id`` are ever touched; an existing value is
never overwritten, so the script is safe to run repeatedly and against a live
database.

Nothing is written unless ``--apply`` is given, which makes the default a
read-only report (``--dry-run`` is accepted as an explicit synonym).

Run from the ``bill-service`` directory::

    python -m scripts.stamp_tenancy                  # report only
    python -m scripts.stamp_tenancy --apply          # persist the backfill
    python -m scripts.stamp_tenancy --apply --laundry-id lovelaundry
    python -m scripts.stamp_tenancy --only bills,gatepasses --apply
    python -m scripts.stamp_tenancy --skip hotel_codes --apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bill_service.database.connection_manager import ROLE_MAIN, get_database
from bill_service.tenancy import LOVE_LAUNDRY_ID, stamp_missing_laundry_ids

MISSING_LAUNDRY_ID = {"$or": [{"laundry_id": {"$exists": False}}, {"laundry_id": None}]}

# Infrastructure collections have no tenant dimension and must never be stamped.
SKIP_COLLECTIONS: Set[str] = {"sync_status", "sync_queue", "sync_logs", "idempotency_keys"}


def select_collections(
    names: Iterable[str],
    only: Optional[Set[str]] = None,
    skip: Optional[Set[str]] = None,
) -> List[str]:
    """Return the sorted collections to stamp.

    When ``only`` is non-empty it acts as an exclusive allow-list. Otherwise
    every collection is selected except ``skip`` plus :data:`SKIP_COLLECTIONS`
    and MongoDB's internal ``system.*`` namespaces.
    """
    if only:
        return sorted(n for n in names if n in only and not n.startswith("system."))
    excluded = set(SKIP_COLLECTIONS) | set(skip or ())
    return sorted(n for n in names if n not in excluded and not n.startswith("system."))


async def stamp_database(
    db: Any,
    laundry_id: str = LOVE_LAUNDRY_ID,
    *,
    apply: bool = False,
    only: Optional[Set[str]] = None,
    skip: Optional[Set[str]] = None,
) -> Dict[str, int]:
    """Stamp every selected collection, returning ``{collection: count}``.

    With ``apply=False`` the counts are computed with ``count_documents`` and
    nothing is written.
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

    def _split(value: str) -> Optional[Set[str]]:
        items = {part.strip() for part in value.split(",") if part.strip()}
        return items or None

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
