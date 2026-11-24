"""Matching a booking to a trip by its dates, with a grace window for transit
(app.services.trip_matching), and the same rule placing transit added by hand
among the trips of a group."""
import uuid
from datetime import date, datetime
from types import SimpleNamespace

from app.services.trip_matching import Stretch, match_trip


def _trip(start, end, created):
    return SimpleNamespace(
        id=uuid.uuid4(),
        start_date=date.fromisoformat(start),
        end_date=date.fromisoformat(end),
        created_at=datetime.fromisoformat(created),
    )


def _flight(departs, arrives):
    return Stretch(datetime.fromisoformat(departs), datetime.fromisoformat(arrives), transit=True)


JAPAN = _trip("2026-04-09", "2026-04-15", "2026-01-01T00:00")


def test_overlap_counts_both_ends_inclusive():
    assert match_trip([JAPAN], [_flight("2026-04-08T23:00", "2026-04-09T06:00")]) is JAPAN
    assert match_trip([JAPAN], [_flight("2026-04-15T23:00", "2026-04-16T06:00")]) is JAPAN


def test_grace_bounds():
    assert match_trip([JAPAN], [_flight("2026-04-08T18:00", "2026-04-08T21:00")]) is JAPAN
    assert match_trip([JAPAN], [_flight("2026-04-08T18:00", "2026-04-08T20:59")]) is None
    assert match_trip([JAPAN], [_flight("2026-04-16T03:00", "2026-04-16T09:00")]) is JAPAN
    assert match_trip([JAPAN], [_flight("2026-04-16T03:01", "2026-04-16T09:00")]) is None
    # Two days out is never in grace, whatever the hour.
    assert match_trip([JAPAN], [_flight("2026-04-07T22:00", "2026-04-07T23:00")]) is None


def test_grace_is_for_transit_only():
    dinner = Stretch(datetime(2026, 4, 8, 22, 0))
    assert match_trip([JAPAN], [dinner]) is None


def test_a_trip_whose_dates_take_it_beats_grace_even_if_newer():
    okinawa = _trip("2026-04-16", "2026-04-18", "2026-02-01T00:00")
    red_eye = _flight("2026-04-16T01:00", "2026-04-16T03:30")
    assert match_trip([JAPAN, okinawa], [red_eye]) is okinawa


def test_the_oldest_wins_among_equals():
    newer = _trip("2026-04-01", "2026-04-20", "2026-03-01T00:00")
    flight = _flight("2026-04-10T09:00", "2026-04-10T12:00")
    assert match_trip([newer, JAPAN], [flight]) is JAPAN
    # Same among grace matches.
    eve = _trip("2026-04-09", "2026-04-12", "2026-03-01T00:00")
    late = _flight("2026-04-08T20:00", "2026-04-08T22:00")
    assert match_trip([eve, JAPAN], [late]) is JAPAN


def test_nothing_to_match_on():
    assert match_trip([JAPAN], []) is None


# --- transit added by hand ---


def _transit(starts_at, ends_at, **extra):
    return {"kind": "transit", "title": "Flight", "starts_at": starts_at, "ends_at": ends_at, **extra}


def _group(client, make_user, make_trip):
    """Japan (Apr 9-15) and its extension Okinawa (Apr 16-18), both with sun
    and amy on them."""
    sun, amy = make_user("sun"), make_user("amy")
    japan = make_trip(sun, members=[{"email": amy.email}])
    okinawa = client.post("/trips", headers=sun, json={
        "title": "Okinawa", "start_date": "2026-04-16", "end_date": "2026-04-18",
        "extends_trip_id": japan["id"], "members": [{"email": amy.email}],
    })
    assert okinawa.status_code == 201, okinawa.text
    return sun, amy, japan, okinawa.json()


def _member_id(trip, email):
    return next(m["id"] for m in trip["members"] if m["email"] == email)


def test_manual_transit_moves_to_the_trip_its_dates_fit(client, make_user, make_trip):
    sun, amy, japan, okinawa = _group(client, make_user, make_trip)
    # Added from Japan, leaving 01:00 on Okinawa's first day: in Japan's grace,
    # but Okinawa's own dates take it. Its people come along as themselves.
    resp = client.post(
        f"/trips/{japan['id']}/items",
        json=_transit(
            "2026-04-16T01:00:00", "2026-04-16T03:30:00",
            participants=[{"member_id": _member_id(japan, amy.email)}],
        ),
        headers=sun,
    )
    assert resp.status_code == 201, resp.text
    item = resp.json()
    assert item["trip_id"] == okinawa["id"]
    assert [p["member"]["id"] for p in item["participants"]] == [_member_id(okinawa, amy.email)]


def test_manual_transit_lands_in_grace_of_another_trip_in_the_group(client, make_user, make_trip):
    sun, _, japan, okinawa = _group(client, make_user, make_trip)
    # Added from Okinawa, landing 22:00 the evening before Japan starts.
    resp = client.post(
        f"/trips/{okinawa['id']}/items",
        json=_transit("2026-04-08T19:00:00", "2026-04-08T22:00:00"),
        headers=sun,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["trip_id"] == japan["id"]


def test_manual_transit_that_fits_no_trip_stays_where_it_was_added(client, make_user, make_trip):
    sun, _, japan, okinawa = _group(client, make_user, make_trip)
    resp = client.post(
        f"/trips/{okinawa['id']}/items",
        json=_transit("2026-05-01T09:00:00", "2026-05-01T12:00:00"),
        headers=sun,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["trip_id"] == okinawa["id"]


def test_manual_non_transit_is_never_moved(client, make_user, make_trip):
    sun, _, japan, _ = _group(client, make_user, make_trip)
    resp = client.post(
        f"/trips/{japan['id']}/items",
        json={"kind": "activity", "title": "Dinner", "starts_at": "2026-04-17T19:00:00"},
        headers=sun,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["trip_id"] == japan["id"]
