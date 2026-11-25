"""Who an imported booking is for: the passenger it names, not whoever
forwarded or uploaded it."""
import uuid
from datetime import timedelta
from types import SimpleNamespace

from app.models import Trip
from app.services.mail_extraction import (
    ExtractedItem,
    ExtractedPlace,
    ExtractedSegment,
    ExtractedTraveler,
    Extraction,
)
from app.services.passengers import match_sender, match_travelers, name_tokens

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _flight(*travelers: ExtractedTraveler, sender_emails=(), confirmation="ABC123") -> Extraction:
    """A flight inside JAPAN_TRIP's dates (2026-04-09 .. 04-15)."""
    return Extraction(
        category="flight", confidence=0.95, summary="LAX to Tokyo",
        sender_emails=list(sender_emails),
        items=[ExtractedItem(
            kind="transit", transit_mode="flight", title="Flight LAX to Tokyo",
            confirmation_number=confirmation, travelers=list(travelers),
            segments=[ExtractedSegment(
                transit_mode="flight", carrier="ANA", flight_number="NH105",
                start_local="2026-04-09T11:00", start_timezone="America/Los_Angeles",
                end_local="2026-04-10T15:00", end_timezone="Asia/Tokyo",
                origin=ExtractedPlace(name="LAX", iata_code="LAX", city="Los Angeles"),
                destination=ExtractedPlace(name="HND", iata_code="HND", city="Tokyo"),
            )],
        )],
    )


def _age(session, trip):
    """The test shares one transaction, so now() stamps every trip alike."""
    session.get(Trip, uuid.UUID(trip["id"])).created_at -= timedelta(days=1)
    session.commit()


def _forward(client, admin, resend, mail_extractor, sender, extraction, email_id="re_p1") -> dict:
    """`sender` forwards a booking; run it through extract and import."""
    resend.received = [{
        "id": email_id, "from": sender.email, "to": ["trips@wravel.app"], "subject": "Fwd: Booking",
        "message_id": f"<{email_id}@x.example>", "created_at": "2026-03-01T12:00:00.000Z",
        "text": "booking details",
    }]
    mail_extractor.result = extraction
    assert client.post("/admin/received/mail", headers=admin).status_code == 200
    r = client.post(f"/admin/received/mail/extract/{email_id}", headers=admin)
    assert r.status_code == 200 and r.json()["status"] == "extracted", r.text
    return client.post(f"/admin/received/mail/import/{email_id}", headers=admin).json()


def _upload(client, image_extractor, uploader, extraction) -> dict:
    image_extractor.result = extraction
    resp = client.post(
        "/received/images/process", files={"file": ("t.png", PNG, "image/png")}, headers=uploader
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _for(client, trip, viewer) -> list[str]:
    """The emails of who the trip's one item is for."""
    items = client.get(f"/trips/{trip['id']}/items", headers=viewer).json()
    assert len(items) == 1, items
    return sorted(p["member"]["email"] for p in items[0]["participants"])


# --- forwarded email ---


def test_email_goes_to_the_passenger_named_in_it_not_the_forwarder(
    client, make_user, admin, make_trip, add_member, resend, mail_extractor
):
    sun, bob = make_user("sun"), make_user("bob")
    trip = make_trip(sun)
    add_member(trip["id"], sun, bob)
    # Bob, a plain member, forwards Sun's ticket.
    body = _forward(client, admin, resend, mail_extractor, bob, _flight(ExtractedTraveler(name="SUN")))
    assert body["outcome"] == "placed" and body["trip_id"] == trip["id"]
    assert _for(client, trip, sun) == ["sun@example.com"]


def test_email_passenger_email_wins_over_their_name(
    client, make_user, admin, make_trip, add_member, resend, mail_extractor
):
    sun, bob, carol = make_user("sun"), make_user("bob"), make_user("carol")
    trip = make_trip(sun)
    add_member(trip["id"], sun, bob)
    add_member(trip["id"], sun, carol)
    traveler = ExtractedTraveler(name="Carol", email="Bob@Example.com")
    _forward(client, admin, resend, mail_extractor, sun, _flight(traveler))
    assert _for(client, trip, sun) == ["bob@example.com"]


def test_email_every_named_passenger_on_the_trip_is_on_the_item(
    client, make_user, admin, make_trip, add_member, resend, mail_extractor
):
    sun, bob, carol = make_user("sun"), make_user("bob"), make_user("carol")
    trip = make_trip(sun)
    add_member(trip["id"], sun, bob)
    add_member(trip["id"], sun, carol)
    travelers = [ExtractedTraveler(name=n) for n in ("Bob", "Carol", "Someone Else")]
    _forward(client, admin, resend, mail_extractor, sun, _flight(*travelers))
    assert _for(client, trip, sun) == ["bob@example.com", "carol@example.com"]


def test_email_matches_a_member_with_no_account_by_email(
    client, make_user, admin, make_trip, resend, mail_extractor
):
    sun = make_user("sun")
    trip = make_trip(sun)
    added = client.post(
        f"/trips/{trip['id']}/members", json={"email": "guest@example.com"}, headers=sun
    )
    assert added.status_code == 201, added.text
    traveler = ExtractedTraveler(name="A Guest", email="guest@example.com")
    _forward(client, admin, resend, mail_extractor, sun, _flight(traveler))
    assert _for(client, trip, sun) == ["guest@example.com"]


def test_email_naming_nobody_falls_back_to_the_first_member_in_the_chain(
    client, make_user, admin, make_trip, add_member, resend, mail_extractor
):
    sun, bob = make_user("sun"), make_user("bob")
    trip = make_trip(sun)
    add_member(trip["id"], sun, bob)
    # The airline sent it to Sun, Sun sent it to Bob, Bob forwarded it to us.
    chain = ["noreply@airline.example", "Sun <sun@example.com>"]
    _forward(client, admin, resend, mail_extractor, bob, _flight(sender_emails=chain))
    assert _for(client, trip, sun) == ["sun@example.com"]


def test_email_naming_only_strangers_falls_back_to_the_chain(
    client, make_user, admin, make_trip, add_member, resend, mail_extractor
):
    sun, bob = make_user("sun"), make_user("bob")
    trip = make_trip(sun)
    add_member(trip["id"], sun, bob)
    extraction = _flight(ExtractedTraveler(name="Not Onthetrip"), sender_emails=["sun@example.com"])
    _forward(client, admin, resend, mail_extractor, bob, extraction)
    assert _for(client, trip, sun) == ["sun@example.com"]


def test_email_with_no_passenger_and_no_chain_is_the_forwarders(
    client, make_user, admin, make_trip, add_member, resend, mail_extractor
):
    sun, bob = make_user("sun"), make_user("bob")
    trip = make_trip(sun)
    add_member(trip["id"], sun, bob)
    _forward(client, admin, resend, mail_extractor, bob, _flight())
    assert _for(client, trip, sun) == ["bob@example.com"]


def test_passenger_picks_between_the_forwarders_overlapping_trips(
    client, make_user, admin, make_trip, add_member, resend, mail_extractor
):
    sun, bob = make_user("sun"), make_user("bob")
    shared = make_trip(sun)
    add_member(shared["id"], sun, bob)
    make_trip(bob)  # Bob's own trip over the same dates; Sun isn't on it
    body = _forward(client, admin, resend, mail_extractor, bob, _flight(ExtractedTraveler(name="Sun")))
    assert body["outcome"] == "placed" and body["trip_id"] == shared["id"]


def test_overlapping_trips_go_to_the_oldest_when_the_passenger_is_the_forwarder(
    client, make_user, admin, make_trip, add_member, resend, mail_extractor, session
):
    sun, bob = make_user("sun"), make_user("bob")
    shared = make_trip(sun)
    add_member(shared["id"], sun, bob)
    make_trip(bob)
    _age(session, shared)
    body = _forward(client, admin, resend, mail_extractor, bob, _flight(ExtractedTraveler(name="Bob")))
    assert body["outcome"] == "placed" and body["trip_id"] == shared["id"]


def test_passenger_beats_the_oldest_of_the_forwarders_overlapping_trips(
    client, make_user, admin, make_trip, add_member, resend, mail_extractor, session
):
    sun, bob = make_user("sun"), make_user("bob")
    _age(session, make_trip(bob))  # older, and Bob's alone
    shared = make_trip(sun)
    add_member(shared["id"], sun, bob)
    body = _forward(client, admin, resend, mail_extractor, bob, _flight(ExtractedTraveler(name="Sun")))
    assert body["outcome"] == "placed" and body["trip_id"] == shared["id"]


# --- image ---


def test_image_goes_to_the_passenger_shown_not_the_uploader(
    client, make_user, make_trip, add_member, image_extractor
):
    sun, bob = make_user("sun"), make_user("bob")
    trip = make_trip(sun)
    add_member(trip["id"], sun, bob)
    body = _upload(client, image_extractor, sun, _flight(ExtractedTraveler(name="Mr Bob")))
    assert body["outcome"] == "placed"
    assert _for(client, trip, sun) == ["bob@example.com"]


def test_image_passenger_email_is_tried_before_the_name(
    client, make_user, make_trip, add_member, image_extractor
):
    sun, bob, carol = make_user("sun"), make_user("bob"), make_user("carol")
    trip = make_trip(sun)
    add_member(trip["id"], sun, bob)
    add_member(trip["id"], sun, carol)
    traveler = ExtractedTraveler(name="Bob", email="carol@example.com")
    _upload(client, image_extractor, sun, _flight(traveler))
    assert _for(client, trip, sun) == ["carol@example.com"]


def test_image_ignores_senders_caught_in_the_picture(
    client, make_user, make_trip, add_member, image_extractor
):
    sun, bob = make_user("sun"), make_user("bob")
    trip = make_trip(sun)
    add_member(trip["id"], sun, bob)
    # A screenshot of a message from Bob, with nobody named on the booking:
    # Bob being in the picture doesn't make it his.
    _upload(client, image_extractor, sun, _flight(sender_emails=["bob@example.com"]))
    assert _for(client, trip, sun) == ["sun@example.com"]


def test_image_naming_nobody_on_the_trip_is_the_uploaders(
    client, make_user, make_trip, image_extractor
):
    sun = make_user("sun")
    trip = make_trip(sun)
    _upload(client, image_extractor, sun, _flight(ExtractedTraveler(name="Somebody Else")))
    assert _for(client, trip, sun) == ["sun@example.com"]


# --- matching ---


def _member(id_, email, display_name=None):
    user = SimpleNamespace(email=email, display_name=display_name) if display_name else None
    return SimpleNamespace(id=id_, email=email, user=user)


def test_name_tokens_ignore_order_case_accents_and_titles():
    assert name_tokens("CHEN/SUN MR") == name_tokens("Sun Chen") == {"sun", "chen"}
    assert name_tokens("José Álvarez") == {"jose", "alvarez"}
    assert name_tokens(None) == frozenset()


def test_name_matches_through_a_middle_name_or_a_short_profile_name():
    sun = _member(1, "sun@example.com", "Sun Chen")
    bob = _member(2, "bob@example.com", "Bob")
    roster = [sun, bob]
    assert match_travelers(roster, [{"name": "Sun Wei Chen"}]) == [sun]
    assert match_travelers(roster, [{"name": "Bob Marlowe"}]) == [bob]
    assert match_travelers(roster, [{"name": "Bobby Marlowe"}]) == []


def test_an_exact_name_beats_a_partial_one_and_a_tie_matches_nobody():
    alex_kim = _member(1, "a@example.com", "Alex Kim")
    alex = _member(2, "b@example.com", "Alex")
    assert match_travelers([alex_kim, alex], [{"name": "Kim Alex"}]) == [alex_kim]
    other_alex = _member(3, "c@example.com", "Alex")
    assert match_travelers([alex, other_alex], [{"name": "Alex Kim"}]) == []


def test_a_member_without_an_account_is_never_matched_by_name():
    guest = _member(1, "guest@example.com")
    assert match_travelers([guest], [{"name": "guest"}]) == []
    assert match_travelers([guest], [{"name": "x", "email": " Guest@Example.com "}]) == [guest]


def test_sender_chain_takes_the_earliest_member():
    sun, bob = _member(1, "sun@example.com", "Sun"), _member(2, "bob@example.com", "Bob")
    chain = ["noreply@airline.example", None, "Bob <bob@example.com>", "sun@example.com"]
    assert match_sender([sun, bob], chain) is bob
    assert match_sender([sun, bob], ["nobody@example.com"]) is None
