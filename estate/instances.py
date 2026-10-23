"""Admin creates and manages realtor instances (F-14).

Each instance owns its own KnowledgeBase, so two instances never share listings,
conversations or client data (N-02) -- isolation comes from each instance holding
a private KnowledgeBase object, not from a shared store keyed by id.

Connected accounts are validated against the shapes the official APIs use (N-03):
a Telegram Bot API chat is a "@channelusername" or a numeric chat id, never a
scraped URL; an Instagram Messaging API account is a plain business-account id.
Rejecting anything else here keeps a profile-URL or scraper endpoint from being
wired in by mistake. Reading posts from these accounts (KAN-21, deferred) and
publishing to them (F-13, backlog) are separate, not-yet-built pieces.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .kb import KnowledgeBase

_TELEGRAM_CHANNEL = re.compile(r"^@\w{5,32}$|^-?\d{5,}$")
_INSTAGRAM_ACCOUNT = re.compile(r"^\d{5,}$|^[A-Za-z0-9._]{1,30}$")


@dataclass
class Instance:
    id: str
    realtor_name: str
    telegram_channel: str = ""
    instagram_account: str = ""
    listing_source: str = ""
    kb: KnowledgeBase = field(default=None, repr=False)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.kb is None:
            self.kb = KnowledgeBase(self.id)


class InstanceRegistry:
    """Every realtor instance the admin has created (F-14)."""

    def __init__(self) -> None:
        self._instances: dict[str, Instance] = {}

    def create(
        self,
        instance_id: str,
        realtor_name: str,
        telegram_channel: str = "",
        instagram_account: str = "",
        listing_source: str = "",
    ) -> Instance:
        if instance_id in self._instances:
            raise ValueError(f"instance {instance_id} already exists")
        if not telegram_channel and not instagram_account:
            raise ValueError("connect a Telegram channel and/or an Instagram account (F-14)")
        if telegram_channel and not _TELEGRAM_CHANNEL.match(telegram_channel):
            raise ValueError(
                f"{telegram_channel!r} is not a Telegram Bot API chat (@channel or numeric id), "
                "the agent only connects through official APIs (N-03)"
            )
        if instagram_account and not _INSTAGRAM_ACCOUNT.match(instagram_account):
            raise ValueError(
                f"{instagram_account!r} is not an Instagram Messaging API account id, "
                "the agent only connects through official APIs (N-03)"
            )
        instance = Instance(instance_id, realtor_name, telegram_channel, instagram_account, listing_source)
        self._instances[instance_id] = instance
        return instance

    def get(self, instance_id: str) -> Instance | None:
        return self._instances.get(instance_id)

    def all(self) -> list[Instance]:
        return list(self._instances.values())
