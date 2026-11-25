"""Working out which trip members an imported booking is for.

Whoever handed us a booking — uploaded the screenshot, forwarded the email — is
not necessarily who it is for: a member forwards a confirmation they were sent
by another member, or uploads a ticket on someone's behalf. So the import stage
looks for the passenger rather than assuming the sender:

  1. The travelers the booking itself names (`ExtractedItem.travelers`), each
     matched to a member by email, then by display name.
  2. Email only, when that finds nobody: the senders along the forwarding chain,
     original message first — the first one who is on the trip. The chain ends
     with the forwarder, so on a trip they're on this always finds someone.

The caller falls back to the receipt's owner when both come up empty (an image
that names nobody, or names only people who aren't on the trip).

Matching is against one trip's roster. A member with no account is known only
by the email they were added at, so a name can only match a linked member.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from email.utils import parseaddr
from typing import Any

from app.models import TripMember

# Titles a ticket prints with a name ("CHEN/SUN MR"); never part of the match.
_TITLES = frozenset({"mr", "mrs", "ms", "miss", "mx", "mstr", "master", "dr", "prof"})
_WORD = re.compile(r"[^\W\d_]+")


def _email(value: Any) -> str | None:
    """Lowercased, as members' and users' emails are stored; None if unusable."""
    if not isinstance(value, str):
        return None
    address = parseaddr(value)[1].strip().lower()  # tolerates "Name <addr>"
    return address if "@" in address else None


def name_tokens(name: Any) -> frozenset[str]:
    """A name as a set of words for comparison: case, accents, punctuation,
    word order ("Chen, Sun" / "Sun Chen") and titles all ignored."""
    if not isinstance(name, str):
        return frozenset()
    plain = "".join(
        ch for ch in unicodedata.normalize("NFKD", name) if not unicodedata.combining(ch)
    )
    return frozenset(_WORD.findall(plain.casefold())) - _TITLES


def _member_emails(member: TripMember) -> set[str]:
    emails = {member.email.lower()}
    if member.user is not None:
        emails.add(member.user.email.lower())
    return emails


def _by_email(members: Sequence[TripMember], email: str | None) -> TripMember | None:
    if email is None:
        return None
    return next((m for m in members if email in _member_emails(m)), None)


def _by_name(members: Sequence[TripMember], name: Any) -> TripMember | None:
    """The one member whose display name is this name. The same words in any
    order is a match; so is one name sitting wholly inside the other (a ticket
    printing a middle name the profile leaves out, or a profile that is just a
    first name). Only ever a single member: if two fit equally well — two
    "Alex"es and a ticket for "Alex Kim" — nobody is picked, since a wrong
    guess puts one person's booking on another."""
    wanted = name_tokens(name)
    if not wanted:
        return None
    exact: list[TripMember] = []
    partial: list[TripMember] = []
    for member in members:
        if member.user is None:
            continue
        have = name_tokens(member.user.display_name)
        if not have:
            continue
        if have == wanted:
            exact.append(member)
        elif have < wanted or wanted < have:
            partial.append(member)
    for found in (exact, partial):
        if found:
            return found[0] if len(found) == 1 else None
    return None


def match_travelers(
    members: Sequence[TripMember], travelers: Iterable[Any]
) -> list[TripMember]:
    """The members among a booking's named travelers, in the booking's order.
    Each traveler goes by their email if it's a member's, else by their name."""
    matched: dict[Any, TripMember] = {}
    for traveler in travelers or []:
        if not isinstance(traveler, dict):
            continue
        member = _by_email(members, _email(traveler.get("email"))) or _by_name(
            members, traveler.get("name")
        )
        if member is not None:
            matched.setdefault(member.id, member)
    return list(matched.values())


def match_sender(members: Sequence[TripMember], chain: Iterable[Any]) -> TripMember | None:
    """The first sender along a forwarding chain (original message first) who is
    on the trip: the member the confirmation was first in the hands of."""
    for address in chain or []:
        member = _by_email(members, _email(address))
        if member is not None:
            return member
    return None
