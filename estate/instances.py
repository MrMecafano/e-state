"""Validated realtor instances, optionally restored from a tenant SQLite store.

With no store the registry retains its original in-memory behavior. A stored
instance owns a write-through KnowledgeBase: ingestion and control messages are
committed automatically. ``get`` and ``all`` refresh stored listings, including
changes committed by another registry/process. Credentials are ENV names only.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .kb import KnowledgeBase, Listing
from .storage import TenantData, TenantStore, _CONFIG_DEFAULTS, _validated_config


@dataclass
class Instance:
    id: str
    realtor_name: str
    telegram_channel: str = ""
    instagram_account: str = ""
    listing_source: str = ""
    kb: KnowledgeBase = field(default=None, repr=False)  # type: ignore[assignment]
    realtor_contact: str = ""
    telegram_token_env: str = ""
    instagram_token_env: str = ""
    webhook_secret_env: str = ""
    instagram_app_secret_env: str = ""
    telegram_admin_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        config = _validated_config(self.id, self._config())
        self.telegram_admin_ids = config["telegram_admin_ids"]
        if self.kb is None:
            self.kb = KnowledgeBase(self.id)

    def _config(self) -> dict:
        return {key: getattr(self, key) for key in _CONFIG_DEFAULTS}


class _StoredKnowledgeBase(KnowledgeBase):
    """Persist normal KB writes without requiring callers to remember to save."""

    def __init__(self, realtor_id: str, scope: TenantData) -> None:
        self._scope = scope
        self._lock = threading.RLock()
        super().__init__(realtor_id, scope.listings())

    def refresh(self) -> None:
        with self._lock:
            self._listings = {listing.id: listing for listing in self._scope.listings()}

    def get(self, listing_id: str) -> Listing | None:
        with self._lock:
            self.refresh()
            return super().get(listing_id)

    def all(self) -> list[Listing]:
        with self._lock:
            self.refresh()
            return super().all()

    def available(self) -> list[Listing]:
        with self._lock:
            self.refresh()
            return super().available()

    def add(self, listing: Listing) -> None:
        with self._lock:
            # Reimporting a source post must not reactivate a control-marked flat.
            self._scope.upsert_listing(listing, preserve_taken=True)
            self.refresh()

    def apply_control_message(self, text: str) -> str:
        with self._lock:
            self.refresh()
            # Compare against this immutable snapshot, never a concurrent cache.
            original = {listing.id: listing for listing in self.all()}
            candidate = KnowledgeBase(self.realtor_id, list(original.values()))
            result = candidate.apply_control_message(text)
            for listing in candidate.all():
                if listing.status != original[listing.id].status:
                    # A status command cannot overwrite media or other metadata,
                    # including fields unknown to an older Listing dataclass.
                    self._scope.set_listing_status(listing.id, listing.status)
            self.refresh()
            return result


class InstanceRegistry:
    """Every realtor instance the admin has created (F-14).

    ``store`` is optional and remains owned by the caller, which must close it.
    Public ``store`` also provides tenant-scoped conversation/event access.
    """

    def __init__(self, store: TenantStore | None = None) -> None:
        self.store = store
        self._instances: dict[str, Instance] = {}

    def create(
        self,
        instance_id: str,
        realtor_name: str,
        telegram_channel: str = "",
        instagram_account: str = "",
        listing_source: str = "",
        realtor_contact: str = "",
        telegram_token_env: str = "",
        instagram_token_env: str = "",
        webhook_secret_env: str = "",
        instagram_app_secret_env: str = "",
        telegram_admin_ids: tuple[str, ...] = (),
    ) -> Instance:
        instance = Instance(
            instance_id,
            realtor_name,
            telegram_channel,
            instagram_account,
            listing_source,
            realtor_contact=realtor_contact,
            telegram_token_env=telegram_token_env,
            instagram_token_env=instagram_token_env,
            webhook_secret_env=webhook_secret_env,
            instagram_app_secret_env=instagram_app_secret_env,
            telegram_admin_ids=telegram_admin_ids,
        )
        if instance_id in self._instances:
            raise ValueError("instance already exists")
        if self.store is not None:
            self.store.create_instance(instance_id, instance._config())
            instance.kb = _StoredKnowledgeBase(instance_id, self.store.scope(instance_id))
        self._instances[instance_id] = instance
        return instance

    def _restore(self, config: dict) -> Instance:
        instance_id = config["id"]
        instance = self._instances.get(instance_id)
        if instance is None:
            assert self.store is not None
            instance = Instance(
                **config,
                kb=_StoredKnowledgeBase(instance_id, self.store.scope(instance_id)),
            )
            self._instances[instance_id] = instance
        elif isinstance(instance.kb, _StoredKnowledgeBase):
            instance.kb.refresh()
        return instance

    def get(self, instance_id: str) -> Instance | None:
        if self.store is not None:
            config = self.store.instance(instance_id)
            return self._restore(config) if config is not None else None
        return self._instances.get(instance_id)

    def all(self) -> list[Instance]:
        if self.store is not None:
            return [self._restore(config) for config in self.store.instances()]
        return list(self._instances.values())
