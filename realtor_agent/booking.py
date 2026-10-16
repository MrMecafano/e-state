"""Viewing calendar with conflict checks."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

SLOT = timedelta(minutes=45)
OPEN, CLOSE = time(9, 0), time(19, 0)


class BookingError(ValueError):
    pass


@dataclass(frozen=True)
class Viewing:
    listing_id: str
    client: str
    start: datetime

    @property
    def end(self) -> datetime:
        return self.start + SLOT


class Calendar:
    def __init__(self) -> None:
        self._viewings: list[Viewing] = []

    def book(self, listing_id: str, client: str, start: datetime) -> Viewing:
        if not client.strip():
            raise BookingError("client name is required")
        if start.time() < OPEN or (start + SLOT).time() > CLOSE:
            raise BookingError("viewings run 09:00-19:00")
        if start.weekday() == 6:
            raise BookingError("no viewings on Sunday")
        for v in self._viewings:
            if v.listing_id == listing_id and start < v.end and v.start < start + SLOT:
                raise BookingError(f"{listing_id} is already booked at {v.start:%H:%M}")
        viewing = Viewing(listing_id, client.strip(), start)
        self._viewings.append(viewing)
        return viewing

    def for_listing(self, listing_id: str) -> list[Viewing]:
        return sorted((v for v in self._viewings if v.listing_id == listing_id), key=lambda v: v.start)
