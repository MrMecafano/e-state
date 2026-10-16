"""Demo CLI: python -m estate.cli chat "2 xonali Yunusobodda 600$ gacha" --budget 600 --district Yunusobod --rooms 2"""
from __future__ import annotations

import argparse

from .agent import handle
from .kb import KnowledgeBase
from .matcher import Criteria

REALTOR_CONTACT = "@demo_realtor"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="estate")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("chat", help="answer one client message")
    c.add_argument("message")
    c.add_argument("--budget", type=int)
    c.add_argument("--district")
    c.add_argument("--rooms", type=int)
    m = sub.add_parser("control", help="realtor control-bot message, e.g. 'L-003 taken'")
    m.add_argument("text")
    args = p.parse_args(argv)

    kb = KnowledgeBase.from_json("demo")
    if args.cmd == "control":
        print(kb.apply_control_message(args.text))
        return 0
    reply, blocked = handle(kb, args.message, Criteria(args.budget, args.district, args.rooms), REALTOR_CONTACT)
    print(reply)
    if blocked:
        print("\n[checker blocked the draft: " + "; ".join(blocked) + "]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
