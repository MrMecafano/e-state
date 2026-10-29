"""Post the demo listings to the demo Telegram channel as Uzbek + Russian e'lons.

    python3 tools/post_demo_listings.py           # print the posts only
    python3 tools/post_demo_listings.py --send    # post them, then save each post link

Titles in data/listings.json are written "Uzbek title / Russian title". The bot
token, ESTATE_DEMO_CHANNEL (numeric -100... ID) and ESTATE_DEMO_REALTOR_CONTACT
come from .env; the bot must be an admin of the channel. Owner names and phones
are never posted. With --send, each listing's post_url is replaced by its post.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from estate.connectors import TelegramConnector  # noqa: E402
from estate.kb import DEMO_DATA  # noqa: E402
from estate.polling import CHANNEL_ENV, CONTACT_ENV, ENV_FILE, TOKEN_ENV, load_env_file  # noqa: E402
from estate.replies import _AMENITIES, _DISTRICT_NAMES  # noqa: E402


def _amenities(row: dict, index: int) -> str:
    names = [_AMENITIES[a][index] if a in _AMENITIES else a for a in row["amenities"]]
    return ", ".join(names)[:1].upper() + ", ".join(names)[1:]


def post_text(row: dict, contact: str) -> str:
    """A bilingual e'lon that estate.ingest.parse_post can also read back."""
    uz_title, _, ru_title = row["title"].partition(" / ")
    district_ru = _DISTRICT_NAMES.get(row["district"], ("", row["district"]))[1]
    lines = [
        f"🏠 {uz_title}",
        f"📍 {row['district']} tumani",
        f"🛏 {row['rooms']} xonali · {row['area_m2']:g} m²",
        f"💵 {row['rent_usd']}$ / oy",
        f"✅ {_amenities(row, 0)}",
        "",
        f"🏠 {ru_title or uz_title}",
        f"📍 Район: {district_ru}",
        f"🛏 {row['rooms']}-комнатная · {row['area_m2']:g} м²",
        f"💵 {row['rent_usd']}$ / месяц",
        f"✅ {_amenities(row, 2)}",
        "",
        f"🆔 {row['id']}",
    ]
    if row["status"] == "taken":
        lines.append("🔴 Band · Занято")
    if contact:
        lines.append(f"📩 {contact}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--send", action="store_true", help="post to the channel and save the links")
    args = parser.parse_args(argv)
    load_env_file(ENV_FILE)
    rows = json.loads(DEMO_DATA.read_text(encoding="utf-8"))
    contact = os.environ.get(CONTACT_ENV, "").strip()
    if not args.send:
        for row in rows:
            print(post_text(row, contact), end="\n\n" + "-" * 40 + "\n\n")
        return 0
    channel = os.environ.get(CHANNEL_ENV, "").strip()
    if not re.fullmatch(r"-100[0-9]+", channel):
        parser.error(f"set {CHANNEL_ENV} in .env to the channel's numeric ID (-100...)")
    connector = TelegramConnector(os.environ.get(TOKEN_ENV, "").strip(), channel=channel)
    for row in rows:
        message = connector.publish(post_text(row, contact))
        # Private-channel links open for channel members only.
        row["post_url"] = f"https://t.me/c/{channel[4:]}/{message['message_id']}"
        print(f"{row['id']} posted: {row['post_url']}")
    DEMO_DATA.write_text("[\n" + ",\n".join("  " + json.dumps(r, ensure_ascii=False) for r in rows) + "\n]\n",
                         encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
