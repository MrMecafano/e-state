"""Command line: search, ask, book."""
from __future__ import annotations

import argparse
from datetime import datetime

from .booking import BookingError, Calendar
from .listings import by_id, load_listings
from .matcher import Criteria, match
from .qa import answer


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="realtor-agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search")
    s.add_argument("--budget", type=int)
    s.add_argument("--district")
    s.add_argument("--rooms", type=int)
    s.add_argument("--need", action="append", default=[])
    a = sub.add_parser("ask")
    a.add_argument("listing_id")
    a.add_argument("question")
    b = sub.add_parser("book")
    b.add_argument("listing_id")
    b.add_argument("client")
    b.add_argument("start", help="YYYY-MM-DDTHH:MM")
    args = p.parse_args(argv)

    listings = load_listings()
    if args.cmd == "search":
        hits = match(listings, Criteria(args.budget, args.district, args.rooms, args.need))
        if not hits:
            print("No listings match. Try a higher budget or another district.")
        for l, sc in hits:
            print(f"{l.id}  ${l.price_usd:>7,}  {l.rooms}r  {l.district:<15} {l.title}  (score {sc})")
        return 0
    listing = by_id(listings, args.listing_id)
    if listing is None:
        print(f"Unknown listing {args.listing_id}")
        return 1
    if args.cmd == "ask":
        print(answer(listing, args.question))
        return 0
    try:
        v = Calendar().book(listing.id, args.client, datetime.fromisoformat(args.start))
    except BookingError as e:
        print(f"Cannot book: {e}")
        return 1
    print(f"Booked {v.client} for {v.listing_id} at {v.start:%a %d %b %H:%M}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
