"""What a run changed, worked out from what it started with.

The worker applies these operations to whatever the list looks like at push
time, rather than overwriting it — because a run overlaps the window in which
someone is clicking, and a run is 30s of a 60s tick.
"""

import json

import pytest

import state


SQUAD = {"booking_id": "b1", "date": "2026-09-21", "time": "5:15 AM", "location": "Marrickville", "name": "Squad"}
PULSE = {"booking_id": "b2", "date": "2026-09-22", "time": "6:00 AM", "location": "Haymarket", "name": "Pulse"}
BY_HAND = {"date": "2026-09-23", "time": "7:00 AM", "location": "Newtown", "name": "Athletica"}


def test_a_booked_class_is_reported_as_a_removal():
    delta = state.pending_delta([SQUAD], [])
    assert delta == {"remove": [{"booking_id": "b1"}]}


def test_a_standing_rule_arming_is_reported_as_an_addition():
    delta = state.pending_delta([], [SQUAD])
    assert delta == {"add": [SQUAD]}


def test_a_strike_outcome_is_reported_as_a_field_update():
    after = {**SQUAD, "state": "watching", "attempts": 1}
    delta = state.pending_delta([SQUAD], [after])
    assert delta == {"update": [{"match": {"booking_id": "b1"}, "fields": {"state": "watching", "attempts": 1}}]}


def test_an_untouched_queue_produces_nothing_to_send():
    assert state.pending_delta([SQUAD, PULSE], [SQUAD, PULSE]) == {}


def test_a_hand_armed_booking_is_matched_without_an_id():
    delta = state.pending_delta([BY_HAND], [])
    assert delta == {"remove": [{"date": "2026-09-23", "time": "7:00 AM", "location": "Newtown", "name": "Athletica"}]}


def test_two_classes_in_the_same_slot_are_told_apart():
    mat = {**BY_HAND, "name": "Mat Pilates"}
    delta = state.pending_delta([BY_HAND, mat], [mat])
    assert delta["remove"] == [{"date": "2026-09-23", "time": "7:00 AM", "location": "Newtown", "name": "Athletica"}]
    assert "add" not in delta


def test_only_the_fields_the_run_writes_are_sent():
    """Anything else differing means the dashboard changed it, and the run has no
    business overwriting that."""
    after = {**SQUAD, "state": "full", "watch_if_full": True, "name": "Renamed"}
    delta = state.pending_delta([SQUAD], [after])
    assert delta["update"][0]["fields"] == {"state": "full"}


def test_rules_report_only_what_they_armed():
    before = [{"id": "r1", "armed_booking_ids": []}]
    after = [{"id": "r1", "armed_booking_ids": ["b1"]}]
    assert state.rules_delta(before, after) == {"armed": [{"match": {"id": "r1"}, "armed_booking_ids": ["b1"]}]}


def test_a_rule_the_run_did_not_touch_is_not_sent():
    rules = [{"id": "r1", "armed_booking_ids": ["b1"]}]
    assert state.rules_delta(rules, rules) == {}


def test_a_rule_created_mid_run_is_not_echoed_back():
    """The bot never creates rules, so a rule that appeared while the run was in
    flight isn't the run's to report."""
    after = [{"id": "r1", "armed_booking_ids": []}, {"id": "new", "armed_booking_ids": []}]
    assert state.rules_delta([{"id": "r1", "armed_booking_ids": []}], after) == {}


# --- pull has to be all-or-nothing and loud ---

def test_pull_writes_all_three_documents(workdir, monkeypatch):
    monkeypatch.setattr(state, "_call", lambda *a, **k: {
        "bookings": [SQUAD], "rules": [{"id": "r1"}], "status": {"status": "SUCCESS"},
    })
    state.pull()
    assert json.loads((workdir / "pending_booking.json").read_text()) == [SQUAD]
    assert json.loads((workdir / "standing_bookings.json").read_text()) == [{"id": "r1"}]
    assert json.loads((workdir / "status.json").read_text()) == {"status": "SUCCESS"}


def test_a_nonsense_response_fails_the_run_rather_than_emptying_the_queue(workdir, monkeypatch):
    """The striker reads a missing or empty queue as "nothing to do" and exits
    green. A bad pull that wrote an empty queue would be a silently missed
    strike, which is the whole failure mode this project keeps hitting."""
    monkeypatch.setattr(state, "_call", lambda *a, **k: {"error": "nope"})
    with pytest.raises(SystemExit):
        state.pull()
    assert not (workdir / "pending_booking.json").exists()


def test_push_refuses_without_a_snapshot(workdir, monkeypatch):
    monkeypatch.setattr(state, "_call", lambda *a, **k: pytest.fail("should not have called the API"))
    with pytest.raises(SystemExit):
        state.push()


def test_push_sends_nothing_when_the_run_changed_nothing(workdir, monkeypatch):
    (workdir / ".state_snapshot.json").write_text(json.dumps({"bookings": [SQUAD], "rules": [], "status": {}}))
    (workdir / "pending_booking.json").write_text(json.dumps([SQUAD]))
    (workdir / "standing_bookings.json").write_text("[]")
    (workdir / "status.json").write_text("{}")
    monkeypatch.setattr(state, "_call", lambda *a, **k: pytest.fail("should not have called the API"))
    assert state.push() == 0
