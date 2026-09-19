"""The striker picks one booking per run and strikes it. Which one it picks is the
whole game: it used to pick a class it had already lost and pick it again, forever,
while everything queued behind it missed its own window."""

import asyncio
import json
from datetime import datetime, timedelta

import pytest
import pytz

import striker

SYD = pytz.timezone("Australia/Sydney")


def slot(hours_out):
    """A class far enough out to clear the 30h margin, whose window is already open."""
    d = datetime.now(SYD) + timedelta(hours=hours_out)
    return {"date": d.strftime("%Y-%m-%d"), "time": d.strftime("%-I:%M %p")}


def booking(booking_id, hours_out, **kw):
    return {**slot(hours_out), "booking_id": booking_id, "location": "Newtown",
            "name": f"Class {booking_id}", **kw}


@pytest.fixture
def strike(workdir, monkeypatch):
    """Run the booker with the network stubbed out, recording what it went for."""
    attempted = []

    def run(queue, outcomes, classes=None):
        (workdir / "pending_booking.json").write_text(json.dumps(queue, indent=2))
        if classes is not None:
            (workdir / "class_list.json").write_text(json.dumps(classes))

        async def fake(target, session):
            attempted.append(target.get("booking_id"))
            return outcomes[target.get("booking_id")]

        monkeypatch.setattr(striker, "try_fast_strike", fake)
        monkeypatch.setattr(striker, "warm_connection", lambda s: None)
        asyncio.run(striker.run_booking())
        return json.loads((workdir / "pending_booking.json").read_text())

    run.attempted = attempted
    return run


FULL = ("FULL", "Fast-path: class is full", None)
WON = ("SUCCESS", None, {"ok": True})


def test_a_full_class_does_not_block_the_next_booking(strike):
    """The bug this guards: selection took the earliest-opening booking and returned
    after striking it, and only a win ever left the queue. One full class was
    re-struck every 60s for ~42h while the booking behind it never got a turn."""
    queue = [booking("full", 40), booking("wanted", 45)]
    outcomes = {"full": FULL, "wanted": WON}

    queue = strike(queue, outcomes)
    assert strike.attempted == ["full"]

    strike(queue, outcomes)
    assert strike.attempted == ["full", "wanted"], "the second booking never got struck"


def test_a_resolved_booking_is_never_struck_again(strike):
    queue = [booking("full", 40)]
    for _ in range(3):
        queue = strike(queue, {"full": FULL})
    assert strike.attempted == ["full"]
    assert queue[0]["state"] == "full"


def test_a_win_leaves_the_queue(strike):
    assert strike([booking("win", 40)], {"win": WON}) == []


def test_repeated_errors_give_up_rather_than_retry_for_days(strike, monkeypatch):
    """Every attempt spends one request against a login endpoint that rate-limits
    at five, so an erroring booking can't be allowed to retry indefinitely."""
    async def boom(target, session):
        strike.attempted.append(target.get("booking_id"))
        raise RuntimeError("network")
    monkeypatch.setattr(striker, "try_fast_strike", boom)
    monkeypatch.setattr(striker, "warm_connection", lambda s: None)

    queue = [booking("flaky", 40)]
    for _ in range(8):
        queue = strike(queue, {})

    assert len(strike.attempted) == 5, "kept retrying past the attempt cap"
    assert queue[0]["state"] == "failed"


def test_a_class_too_close_to_start_is_never_struck(strike):
    """Cancelling inside 24h costs money, so a class the bot couldn't safely cancel
    out of is one it must not book in the first place."""
    queue = strike([booking("soon", 20)], {"soon": WON})
    assert strike.attempted == []
    assert len(queue) == 1, "the booking should stay queued, just untouched"


# --- watching a full class for a cancellation ---

def klass(booking_id, hours_out, spots):
    return {**slot(hours_out), "booking_id": booking_id, "location": "Newtown",
            "name": f"Class {booking_id}", "remaining_spots": spots}


def test_watching_costs_nothing_until_a_spot_opens(strike):
    queue = [booking("hot", 40, watch_if_full=True)]

    queue = strike(queue, {"hot": FULL}, classes=[klass("hot", 40, 0)])
    assert queue[0]["state"] == "watching"

    for _ in range(5):
        queue = strike(queue, {"hot": FULL}, classes=[klass("hot", 40, 0)])
    assert strike.attempted == ["hot"], "watching spent booking attempts while the class was full"

    queue = strike(queue, {"hot": WON}, classes=[klass("hot", 40, 1)])
    assert strike.attempted == ["hot", "hot"]
    assert queue == [], "the freed spot wasn't taken"


def test_without_the_flag_a_full_class_stays_resolved(strike):
    queue = strike([booking("cold", 40)], {"cold": FULL}, classes=[klass("cold", 40, 5)])
    assert queue[0]["state"] == "full"


def test_watching_needs_a_booking_id_to_match_on(strike):
    """A hand-entered booking has no id, so there is nothing to match it against in
    the class list — it resolves rather than sitting in the queue forever waiting
    for a free spot it has no way to see."""
    hand = {**slot(40), "location": "Newtown", "watch_if_full": True}
    queue = strike([hand], {None: FULL}, classes=[])
    assert queue[0]["state"] == "full"


# --- telemetry ---

def test_records_how_late_the_strike_actually_fired(strike, workdir):
    """Warming the connection and sleeping to the exact second are the whole design;
    this is the number they exist to move."""
    strike([booking("win", 40)], {"win": WON})
    status = json.loads((workdir / "status.json").read_text())
    assert isinstance(status["strike_latency_ms"], int)
    assert isinstance(status["strike_took_ms"], int)
    assert status["path"] == "fast"


# --- input handling ---

def test_a_half_written_queue_reports_itself(workdir, monkeypatch):
    (workdir / "pending_booking.json").write_text('[{"date": "2026-09-2')
    asyncio.run(striker.run_booking())
    assert json.loads((workdir / "status.json").read_text())["status"] == "ERROR"


def test_parse_class_time_accepts_both_formats():
    assert striker.parse_class_time("6:00 AM") == (6, 0, "AM")
    assert striker.parse_class_time("6.30AM") == (6, 30, "AM")
    assert striker.parse_class_time("not a time") is None
