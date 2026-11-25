"""Retrieve stage: fetch received emails from Resend and store them, nothing
more — no sender check, no extraction. Stored rows start at status `received`;
the extract stage advances them.

Both entry points (single-by-id and recent-list) go through `_upsert_received`,
keyed on the unique `resend_email_id` so re-retrieving is idempotent. The
single-email retrieve has the full body and persists it in `raw`; the list has
metadata only.
"""
import uuid
from datetime import UTC, datetime
from email.utils import parseaddr
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import EmailReceipt, EmailReceiptStatus
from app.services.mail_processing import InboundEmail


def trip_inboxes() -> frozenset[str]:
    """The addresses this server takes forwarded mail for (`TRIP_INBOXES`):
    production's inbox on the production server, a dev one elsewhere. A Resend
    account hands every environment the same mail, whatever address it was sent
    to, so mail for another environment's inbox is left alone."""
    configured = get_settings().trip_inboxes.split(",")
    return frozenset(address.strip().lower() for address in configured if address.strip())


def sent_to_trip_inbox(data: dict[str, Any]) -> bool:
    """Whether an email (a webhook's `data` or a list/retrieve object) names
    this server's trips inbox among its recipients: To, Cc or Bcc, each of
    which may be `Name <addr>`."""
    inboxes = trip_inboxes()
    for field in ("to", "cc", "bcc"):
        recipients = data.get(field)
        if isinstance(recipients, str):
            recipients = [recipients]
        if not isinstance(recipients, list):
            continue
        for recipient in recipients:
            if isinstance(recipient, str) and (
                parseaddr(recipient)[1].strip().lower() in inboxes
            ):
                return True
    return False


def _upsert_received(
    session: Session, inbound: InboundEmail, raw: dict[str, Any] | None = None
) -> EmailReceipt:
    """Insert a `received` receipt, keyed on resend_email_id. A row we've already
    seen is left as-is (its status/metadata aren't rewound) — except that a
    re-fetch carrying the body refreshes `raw`, so the single-email retrieve can
    fill in a body first seen without one."""
    values = {
        "id": uuid.uuid4(),
        "resend_email_id": inbound.email_id,
        "status": EmailReceiptStatus.RECEIVED,
        "from_address": inbound.from_address,
        "subject": inbound.subject,
        "message_id": inbound.message_id,
        "received_at": inbound.received_at,
        "raw": raw,
    }
    stmt = pg_insert(EmailReceipt).values(**values)
    if raw is not None:
        stmt = stmt.on_conflict_do_update(
            index_elements=["resend_email_id"],
            set_={"raw": stmt.excluded.raw, "updated_at": datetime.now(UTC)},
        )
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=["resend_email_id"])
    session.execute(stmt)
    return session.scalar(
        select(EmailReceipt).where(EmailReceipt.resend_email_id == inbound.email_id)
    )


def store_received_email(session: Session, email: dict[str, Any]) -> EmailReceipt | None:
    """Store one fully-retrieved email (Resend's retrieve response, body and
    all). None if the payload has no id."""
    inbound = InboundEmail.from_list_item(email)
    if inbound is None:
        return None
    return _upsert_received(session, inbound, raw=email)


def store_received_list(session: Session, emails: list[Any]) -> list[EmailReceipt]:
    """Store a page of listed emails (metadata only — the list has no body).
    Ones not sent to the trips inbox are passed over, as the webhook does."""
    stored: list[EmailReceipt] = []
    seen: set[str] = set()
    for item in emails:
        if not isinstance(item, dict) or not sent_to_trip_inbox(item):
            continue
        inbound = InboundEmail.from_list_item(item)
        if inbound is None or inbound.email_id in seen:
            continue
        seen.add(inbound.email_id)
        stored.append(_upsert_received(session, inbound))
    return stored


def store_received_webhook(session: Session, data: dict[str, Any]) -> EmailReceipt | None:
    """Store the email an `email.received` webhook announces (its `data`:
    metadata only — the extract stage fetches the body). None if it names no
    email id."""
    inbound = InboundEmail.from_webhook(data)
    if inbound is None:
        return None
    return _upsert_received(session, inbound)
