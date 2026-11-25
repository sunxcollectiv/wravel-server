"""Import stage: turn an ``extracted`` receipt (email or image) into trip item(s)
in an existing trip the owner is a member of, updating the receipt to
``processed``.

Source-neutral: an ``EmailReceipt`` and an ``ImageReceipt`` carry the same fields
this stage touches (``user_id`` — the matched sender or the uploader —,
``status``, ``extracted``, ``trip_id``), and the extracted booking has the same
shape whatever produced it, so one importer serves both. The per-source routes
pass their own receipt in.

The trip is matched by the booking's dates among the trips the owner is on,
in any role (see app.services.trip_matching: date overlap first, then a grace
window for transit landing the evening before or leaving the small hours after
a trip). When several trips match equally, the booking's passengers settle it
if they can: the one trip that someone other than the owner is matched on.
Otherwise the oldest wins. No match leaves the receipt ``extracted`` with a
``no_match`` outcome, to be re-run once a trip fits. Creating a new trip is
out of scope.

Who each item is for is worked out against the chosen trip's roster rather than
assumed to be the owner, who may be passing on someone else's booking (see
`app.services.passengers`): the travelers the booking names, matched to members
by email and then display name; for an email with none of those on the trip, the
first sender along its forwarding chain who is; failing that, the owner. The
owner stays the item's creator either way.

Lodging: a stay whose confirmation gives only the check-in/check-out *dates*
gets the conventional times — check-in 15:00, check-out 11:00 — rather than
midnight; one with no check-out (or a same-day one) is assumed to be one
night; its details (property type, phone, cancellation deadline) are
cleaned up onto the item's lodging row, and what it says about rooms becomes
the item's rooms (see `_rooms`). It's all the user's to edit afterwards.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.trip_items import build_trip_item
from app.models import (
    EmailReceipt,
    EmailReceiptStatus,
    ImageReceipt,
    LodgingType,
    Trip,
    TripItem,
    TripItemSource,
    TripMember,
    User,
)
from app.schemas import ParticipantSet, TripItemCreate
from app.services.passengers import match_sender, match_travelers
from app.services.place_resolver import PlaceCache, resolve_place
from app.services.trip_matching import Stretch, matching_trips, search_window

Receipt = EmailReceipt | ImageReceipt

Outcome = Literal["placed", "no_match", "skipped"]

# The statuses the extract step will (re)run on; terminal ones are left alone, so
# process is safe to re-run. Mirrors the per-source EXTRACTABLE_STATUSES.
EXTRACTABLE_STATUSES = (EmailReceiptStatus.RECEIVED, EmailReceiptStatus.PENDING)


@dataclass
class ImportOutcome:
    receipt: Receipt
    outcome: Outcome
    trip_id: Any = None  # uuid.UUID | None
    item_count: int = 0


# What a stay's ends default to when the booking states only the dates.
CHECK_IN_TIME = time(15, 0)
CHECK_OUT_TIME = time(11, 0)


def _as_datetime(local: Any, *, date_only_at: time = time(0)) -> datetime | None:
    """Parse an extracted local time. A bare date ("2026-04-10") is taken at
    `date_only_at` on that day."""
    if not isinstance(local, str):
        return None
    local = local.strip()
    try:
        parsed = datetime.fromisoformat(local)
    except ValueError:
        return None
    if "T" not in local and " " not in local:  # a bare date
        return datetime.combine(parsed.date(), date_only_at)
    return parsed


def _booking_stretches(extraction: dict[str, Any]) -> list[Stretch]:
    """Each item of the booking as the trip matcher sees it. A transit leaves
    when its first leg does and lands when its last leg does; an item none of
    whose times can be read is left out."""
    stretches: list[Stretch] = []
    for item in extraction.get("items") or []:
        parts: list[dict[str, Any]] = item.get("segments") or [item]
        moments = [
            moment
            for part in parts
            for key in ("start_local", "end_local")
            if (moment := _as_datetime(part.get(key))) is not None
        ]
        if not moments:
            continue
        departs = _as_datetime(parts[0].get("start_local")) or min(moments)
        arrives = _as_datetime(parts[-1].get("end_local")) or max(moments)
        stretches.append(
            Stretch(starts_at=departs, ends_at=arrives, transit=item.get("kind") == "transit")
        )
    return stretches


def _roster(session: Session, trip: Trip) -> list[TripMember]:
    return list(
        session.scalars(
            select(TripMember)
            .where(TripMember.trip_id == trip.id)
            .options(selectinload(TripMember.user))
            .order_by(TripMember.created_at, TripMember.id)
        )
    )


def _sender_chain(receipt: Receipt, extraction: dict[str, Any]) -> list[Any]:
    """An email's senders, original message first: the ones read out of its
    quoted headers, then whoever actually sent it to us. An image has no chain —
    a sender visible in a screenshot is part of the picture, not of how the
    booking reached us."""
    if not isinstance(receipt, EmailReceipt):
        return []
    return [*(extraction.get("sender_emails") or []), receipt.from_address]


def _passengers(
    roster: list[TripMember], item: dict[str, Any], chain: list[Any]
) -> list[TripMember]:
    """The members one extracted item is for: the travelers it names who are on
    the trip, else the first sender along the chain who is. Empty when neither
    finds anyone (the caller falls back to the receipt's owner)."""
    named = match_travelers(roster, item.get("travelers"))
    if named:
        return named
    sender = match_sender(roster, chain)
    return [sender] if sender is not None else []


def _match_trip(session: Session, receipt: Receipt, extraction: dict[str, Any]) -> Trip | None:
    """The member-trip the booking belongs to (see app.services.trip_matching).
    Among several that match equally, it's the only one where the booking is
    for someone besides the owner: being on all of them, the owner can't tell
    them apart, but a fellow passenger who is on just one can. Failing that,
    the oldest."""
    stretches = _booking_stretches(extraction)
    if not stretches:
        return None
    first, last = search_window(stretches)
    member_trip_ids = select(TripMember.trip_id).where(TripMember.user_id == receipt.user_id)
    nearby = session.scalars(
        select(Trip).where(
            Trip.id.in_(member_trip_ids), Trip.start_date <= last, Trip.end_date >= first
        )
    )
    candidates = matching_trips(nearby, stretches)
    if len(candidates) <= 1:
        return candidates[0] if candidates else None
    chain = _sender_chain(receipt, extraction)
    told_apart = [
        trip
        for trip in candidates
        if any(
            member.user_id != receipt.user_id
            for item in extraction.get("items") or []
            for member in _passengers(_roster(session, trip), item, chain)
        )
    ]
    return told_apart[0] if len(told_apart) == 1 else candidates[0]


def _ends(
    part: dict[str, Any], *, start_default: time = time(0), end_default: time = time(0)
) -> dict[str, Any]:
    """The part's two ends; a date-only end is taken at the given default."""
    return {
        "starts_at": _as_datetime(part.get("start_local"), date_only_at=start_default),
        "start_timezone": part.get("start_timezone"),
        "ends_at": _as_datetime(part.get("end_local"), date_only_at=end_default),
        "end_timezone": part.get("end_timezone"),
    }


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip()[:limit] or None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and 1 <= value <= 999 else None


def _lodging_details(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Clean an extracted stay's details into a `LodgingDetails` payload, or
    None when nothing usable was stated. Lenient: an unknown property type
    becomes `other` — an odd value loses that field, never the booking. A
    date-only cancellation deadline is taken at the start of that day, the
    cautious reading of "free cancellation until <date>"."""
    if not raw:
        return None
    kind = _text(raw.get("property_type"), 32)
    known = {t.value for t in LodgingType}
    details = {
        "property_type": (kind if kind in known else LodgingType.OTHER.value) if kind else None,
        "phone": _text(raw.get("phone"), 40),
        "free_cancellation_until": _as_datetime(raw.get("free_cancellation_until")),
    }
    return details if any(v is not None for v in details.values()) else None


# A booking "for 30 rooms" is a group block, not something to lay out room by room.
MAX_SUMMARY_ROOMS = 20


def _rooms(raw: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """An extracted stay's rooms as `rooms` payload entries, or None when the
    booking says nothing about rooms. Itemized rooms are taken as listed;
    otherwise the booking's summary — room count, room type, guests — becomes
    that many rooms of that type (the guest count only when it's one room,
    since a total can't be split). Nobody is put in a room: guests are printed
    by name and members are known by email, so people are placed by hand."""
    raw = raw or {}
    listed = raw.get("rooms")
    if isinstance(listed, list) and listed:
        rooms = [
            {
                "label": _text(room.get("label"), 60),
                "room_number": _text(room.get("room_number"), 40),
                "room_type": _text(room.get("room_type"), 200),
                "guests": _count(room.get("guests")),
                "confirmation_number": _text(room.get("confirmation_number"), 120),
            }
            for room in listed[:50]
            if isinstance(room, dict)
        ]
        return rooms or None

    count = _count(raw.get("room_count"))
    room_type, guests = _text(raw.get("room_type"), 200), _count(raw.get("guests"))
    if count is None and room_type is None and guests is None:
        return None
    count = count or 1
    if count > MAX_SUMMARY_ROOMS:
        return None
    return [
        {"room_type": room_type, "guests": guests if count == 1 else None}
        for _ in range(count)
    ]


def _item_payload(
    session: Session,
    item: dict[str, Any],
    *,
    user_id: Any,
    cache: PlaceCache,
    participants: list[TripMember],
) -> TripItemCreate:
    """Map one extracted item to a create payload, resolving its places (airports/
    hotels) to Place ids via the shared resolver as we go. `participants` are
    the members it's for."""
    kind = item.get("kind") or "activity"

    def place_id(
        descriptor: dict[str, Any] | None, tz: str | None, mode: str | None = None
    ) -> Any:
        # On a flight the extracted code is an IATA airport code, so the place
        # resolves by it (to the seeded airport). Other modes' codes (a station)
        # aren't airports, so they match by name and address as before.
        return resolve_place(
            session,
            descriptor,
            timezone=tz,
            created_by_id=user_id,
            cache=cache,
            code_category="airport" if mode == "flight" else None,
        )

    values: dict[str, Any] = {
        "kind": kind,
        "title": (item.get("title") or "Booking")[:200],
        "confirmation_number": _text(item.get("confirmation_number"), 120),
        "booking_number": _text(item.get("booking_number"), 120),
        "participants": [ParticipantSet(member_id=member.id) for member in participants],
    }
    segments = item.get("segments") or []
    if kind == "transit" and segments:
        values["segments"] = [
            {
                **_ends(seg),
                "carrier": seg.get("carrier"),
                "flight_number": seg.get("flight_number"),
                "transit_mode": seg.get("transit_mode"),
                "confirmation_number": seg.get("confirmation_number"),
                "place_id": place_id(
                    seg.get("origin"),
                    seg.get("start_timezone"),
                    seg.get("transit_mode") or item.get("transit_mode"),
                ),
                "end_place_id": place_id(
                    seg.get("destination"),
                    seg.get("end_timezone"),
                    seg.get("transit_mode") or item.get("transit_mode"),
                ),
            }
            for seg in segments
        ]
    elif kind == "lodging":
        values.update(_ends(item, start_default=CHECK_IN_TIME, end_default=CHECK_OUT_TIME))
        # A stay always checks out on a later date than it checks in. With no
        # check-out stated, or a same-day (or earlier) one — a misread, or a
        # day-use booking we don't model — assume one night, check-out 11:00
        # the next day, for the user to correct, rather than failing the
        # whole import.
        start, end = values["starts_at"], values["ends_at"]
        if start is not None and (end is None or end.date() <= start.date()):
            values["ends_at"] = datetime.combine(start.date() + timedelta(days=1), CHECK_OUT_TIME)
            values["end_timezone"] = values["start_timezone"]
        # A stay is at one property: the check-out end has no place of its own.
        values["place_id"] = place_id(item.get("place"), item.get("start_timezone"))
        values["lodging"] = _lodging_details(item.get("lodging"))
        values["rooms"] = _rooms(item.get("lodging"))
    else:
        values.update(_ends(item))
        mode = item.get("transit_mode") if kind == "transit" else None
        values["place_id"] = place_id(item.get("place"), item.get("start_timezone"), mode)
        values["end_place_id"] = place_id(item.get("end_place"), item.get("end_timezone"), mode)
        if kind == "transit":
            values["transit_mode"] = item.get("transit_mode")
    return TripItemCreate(**values)


def _link_source(item: TripItem, receipt: Receipt) -> None:
    """Mark an imported item with where it came from, linking the receipt so
    its participants can pull up the original."""
    if isinstance(receipt, EmailReceipt):
        item.source, item.source_email_receipt_id = TripItemSource.EMAIL, receipt.id
    else:
        item.source, item.source_image_id = TripItemSource.IMAGE, receipt.id


def import_receipt(session: Session, receipt: Receipt) -> ImportOutcome:
    """Place an extracted receipt's booking into the owner's matching trip. Only
    acts on `extracted` receipts; the caller commits (atomic per receipt)."""
    if receipt.status is not EmailReceiptStatus.EXTRACTED:
        return ImportOutcome(receipt, "skipped")

    extraction = receipt.extracted or {}
    trip = _match_trip(session, receipt, extraction)
    if trip is None:
        return ImportOutcome(receipt, "no_match")

    # TODO(monetization): refuse placing into a locked/unpaid trip here — a trip
    # the user hasn't paid for should block this step. See [[monetization-model]].
    user = session.get(User, receipt.user_id)
    cache: PlaceCache = {}  # one place per booking (a layover airport resolves once)
    roster = _roster(session, trip)
    chain = _sender_chain(receipt, extraction)
    owner = [member for member in roster if member.user_id == receipt.user_id]
    count = 0
    for item in extraction.get("items") or []:
        payload = _item_payload(
            session,
            item,
            user_id=receipt.user_id,
            cache=cache,
            participants=_passengers(roster, item, chain) or owner,
        )
        _link_source(build_trip_item(session, trip, user, payload), receipt)
        count += 1

    receipt.status = EmailReceiptStatus.PROCESSED
    receipt.trip_id = trip.id
    return ImportOutcome(receipt, "placed", trip_id=trip.id, item_count=count)


def process_receipt(
    session: Session, receipt: Receipt, *, extract: Callable[[], None]
) -> ImportOutcome:
    """Run the full pipeline on one receipt in a single pass: extract (if it's
    still awaiting it), then import. `extract` is the source-specific extract
    step as a no-arg callable (mail needs Resend + its extractor, image just its
    extractor), run in place on the receipt. Import only fires when extract left
    the receipt `extracted`, so a non-booking/low-confidence/ignored/errored
    receipt comes back with that status and a `skipped` outcome. Safe to re-run:
    a terminal receipt skips extract, and import no-ops once it's `processed`.
    The caller commits; the returned `ImportOutcome` carries the final state the
    UI needs (receipt status + placement outcome)."""
    if receipt.status in EXTRACTABLE_STATUSES:
        extract()
    return import_receipt(session, receipt)
