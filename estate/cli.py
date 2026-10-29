"""Demo CLI: python -m estate.cli chat "2 xonali Yunusobodda 600$ gacha"

Budget, district and rooms are read from the message; the flags only override them.
"""
from __future__ import annotations

import argparse

from .agent import handle
from .extract import extract
from .kb import KnowledgeBase
from .matcher import Criteria

REALTOR_CONTACT = "@demo_realtor"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="estate")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("chat", help="answer one client message")
    c.add_argument("message")
    c.add_argument("--budget", type=int, help="override the budget read from the message")
    c.add_argument("--district", help="override the district read from the message")
    c.add_argument("--rooms", type=int, help="override the room count read from the message")
    m = sub.add_parser("control", help="realtor control-bot message, e.g. 'L-003 taken'")
    m.add_argument("text")
    args = p.parse_args(argv)

    kb = KnowledgeBase.from_json("demo")
    if args.cmd == "control":
        print(kb.apply_control_message(args.text))
        return 0
    found = extract(args.message)
    criteria = Criteria(found.budget_usd if args.budget is None else args.budget,
                        args.district or found.district,
                        found.rooms if args.rooms is None else args.rooms)
    reply, blocked = handle(kb, args.message, criteria, REALTOR_CONTACT)
    print(reply)
    if blocked:
        print("\n[checker blocked the draft: " + "; ".join(blocked) + "]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
