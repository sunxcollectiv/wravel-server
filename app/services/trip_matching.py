"""Which trip a booking belongs to, by its dates: shared by receipt import
(across all of a person's trips) and by adding a transit item by hand (across
the trips in the group it was added to).

A trip matches in one of two ways, and the first kind always wins:

1. Its dates overlap the booking's: the trip runs on or between the dates
   anything in the booking starts and ends, by local wall time. A flight that
   leaves on a trip's last day, or lands on its first, overlaps.
2. Grace, for transit only: it lands on the evening before the trip starts
   (at or after 21:00), or leaves in the small hours of the day after it ends
   (at or before 03:00). So a late arrival or a red-eye home still lands in
   the trip it serves, but loses to a trip whose own dates take it, such as
   the extension it actually flies into.

Several trips matching the same way go to the oldest (the first created).
Times are local wall times throughout, as they appear on the booking.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app.models import Trip

# A transit landing at or after this on the day before a trip starts belongs to it.
ARRIVAL_GRACE_FROM = time(21, 0)
# A transit leaving at or before this on the day after a trip ends belongs to it.
DEPARTURE_GRACE_UNTIL = time(3, 0)


@dataclass(frozen=True)
class Stretch:
    """One item of a booking as the matcher sees it: when it starts and ends
    (local wall times; a one-ended item ends when it starts), and whether it is
    transit, which alone gets the grace window. For transit, `starts_at` is the
    departure and `ends_at` the arrival."""

    starts_at: datetime
    ends_at: datetime | None = None
    transit: bool = False

    @property
    def last(self) -> datetime:
        return self.ends_at or self.starts_at


def _overlaps(trip: Trip, stretches: Sequence[Stretch]) -> bool:
    """Whether the trip's dates touch the booking's, ends included."""
    days = [moment.date() for s in stretches for moment in (s.starts_at, s.last)]
    return trip.start_date <= max(days) and trip.end_date >= min(days)


def _in_grace(trip: Trip, stretch: Stretch) -> bool:
    """Whether a transit lands the evening before the trip or leaves in the
    small hours after it."""
    if not stretch.transit:
        return False
    arrives, departs = stretch.last, stretch.starts_at
    eve = trip.start_date - timedelta(days=1)
    morning_after = trip.end_date + timedelta(days=1)
    return (arrives.date() == eve and arrives.time() >= ARRIVAL_GRACE_FROM) or (
        departs.date() == morning_after and departs.time() <= DEPARTURE_GRACE_UNTIL
    )


def _oldest(trips: Iterable[Trip]) -> Trip | None:
    return min(trips, key=lambda trip: (trip.created_at, str(trip.id)), default=None)


def match_trip(trips: Iterable[Trip], stretches: Sequence[Stretch]) -> Trip | None:
    """The trip of `trips` the booking belongs to (see the module doc), or None
    when none fits or the booking has no times at all."""
    if not stretches:
        return None
    trips = list(trips)
    return _oldest(t for t in trips if _overlaps(t, stretches)) or _oldest(
        t for t in trips if any(_in_grace(t, s) for s in stretches)
    )


def search_window(stretches: Sequence[Stretch]) -> tuple[date, date]:
    """The trip dates worth loading for these stretches: a trip ending before
    the first or starting after the last can't match, even by grace."""
    days = [moment.date() for s in stretches for moment in (s.starts_at, s.last)]
    return min(days) - timedelta(days=1), max(days) + timedelta(days=1)
