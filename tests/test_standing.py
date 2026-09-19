"""Standing rules arm classes on their own, so the ways they can be wrong are the
ways you end up booked into something you never chose."""

import json

import pytest

import standing


def write(path, value):
    path.write_text(json.dumps(value, indent=2))


@pytest.fixture
def timetable(workdir):
    """Two Mondays and a Tuesday, same slot, one location."""
    classes = [
        {"booking_id": "b1", "date": "2026-09-21", "time": "6:00 AM", "location": "Newtown",
         "name": "Athletica", "class_type": "S&C Series", "instructor": "Ada", "remaining_spots": 4},
        {"booking_id": "b2", "date": "2026-09-28", "time": "6:00 AM", "location": "Newtown",
         "name": "Athletica", "class_type": "S&C Series", "instructor": "Ada", "remaining_spots": 4},
        {"booking_id": "b3", "date": "2026-09-22", "time": "6:00 AM", "location": "Newtown",
         "name": "Reformer: Strong", "class_type": "Reformer Series", "instructor": "Bo", "remaining_spots": 2},
    ]
    write(workdir / "class_list.json", classes)
    write(workdir / "pending_booking.json", [])
    return workdir


def rule(**kw):
    base = {"id": "weekly", "weekday": "Monday", "time": "6:00 AM", "location": "Newtown",
            "name": "Athletica", "enabled": True}
    base.update(kw)
    return base


def queued(workdir):
    return json.loads((workdir / "pending_booking.json").read_text())


def test_arms_every_occurrence_of_the_rule(timetable):
    write(timetable / "standing_bookings.json", [rule()])
    standing.arm_standing_bookings()
    assert {b["booking_id"] for b in queued(timetable)} == {"b1", "b2"}


def test_does_not_arm_the_same_class_twice(timetable):
    write(timetable / "standing_bookings.json", [rule()])
    standing.arm_standing_bookings()
    standing.arm_standing_bookings()
    assert len(queued(timetable)) == 2


def test_removing_an_auto_armed_booking_sticks(timetable):
    """The matcher runs every 60s. Without a memory of what it has armed, deleting
    one from the dashboard just means it reappears a minute later, forever."""
    write(timetable / "standing_bookings.json", [rule()])
    standing.arm_standing_bookings()
    write(timetable / "pending_booking.json", [])

    standing.arm_standing_bookings()
    assert queued(timetable) == []


def test_forgets_ids_once_they_leave_the_timetable(timetable):
    """Otherwise the rule's memory grows forever. An id can't come back once the
    class ages out of the list, so forgetting it can't cause a re-arm."""
    write(timetable / "standing_bookings.json", [rule()])
    standing.arm_standing_bookings()
    assert json.loads((timetable / "standing_bookings.json").read_text())[0]["armed_booking_ids"]

    # the timetable rolls forward: b1/b2 are now inside the 30h margin and gone
    write(timetable / "class_list.json", [
        {"booking_id": "b9", "date": "2026-10-05", "time": "6:00 AM", "location": "Newtown",
         "name": "Athletica", "class_type": "S&C Series", "instructor": "Ada", "remaining_spots": 4},
    ])
    standing.arm_standing_bookings()
    assert json.loads((timetable / "standing_bookings.json").read_text())[0]["armed_booking_ids"] == ["b9"]


def test_an_empty_class_list_is_not_treated_as_an_empty_timetable(timetable):
    """The scraper refuses to write a list it doesn't trust, so an empty one means
    "no information" — forgetting armed ids there would re-arm everything."""
    write(timetable / "standing_bookings.json", [rule()])
    standing.arm_standing_bookings()
    write(timetable / "class_list.json", [])

    standing.arm_standing_bookings()
    assert json.loads((timetable / "standing_bookings.json").read_text())[0]["armed_booking_ids"] == ["b1", "b2"]


def test_a_disabled_rule_arms_nothing(timetable):
    write(timetable / "standing_bookings.json", [rule(enabled=False)])
    standing.arm_standing_bookings()
    assert queued(timetable) == []


def test_an_unpinned_rule_is_refused(timetable):
    """"Mondays at Newtown" with no time matched a whole day's timetable — 58
    bookings from one rule, each one a class you'd be charged for missing."""
    write(timetable / "standing_bookings.json", [rule(time=None, name=None)])
    standing.arm_standing_bookings()
    assert queued(timetable) == []


def test_arming_stops_at_the_queue_cap(timetable, monkeypatch):
    monkeypatch.setattr(standing, "MAX_PENDING", 1)
    write(timetable / "standing_bookings.json", [rule()])
    standing.arm_standing_bookings()
    assert len(queued(timetable)) == 1


def test_rule_fields_narrow_the_match(timetable):
    write(timetable / "standing_bookings.json", [rule(weekday="Tuesday", name="Reformer: Strong")])
    standing.arm_standing_bookings()
    assert [b["booking_id"] for b in queued(timetable)] == ["b3"]


def test_carries_the_watch_flag_onto_the_booking(timetable):
    write(timetable / "standing_bookings.json", [rule(watch_if_full=True)])
    standing.arm_standing_bookings()
    assert all(b["watch_if_full"] for b in queued(timetable))


def test_leaves_a_hand_armed_booking_alone(timetable):
    mine = {"booking_id": "b1", "date": "2026-09-21", "time": "6:00 AM", "location": "Newtown", "name": "Athletica"}
    write(timetable / "pending_booking.json", [mine])
    write(timetable / "standing_bookings.json", [rule()])
    standing.arm_standing_bookings()
    assert queued(timetable)[0] == mine
    assert len(queued(timetable)) == 2


def test_accepts_a_single_booking_object(timetable):
    """pending_booking.json is allowed to hold one bare object — the striker reads
    that shape too — so the matcher normalises rather than bailing."""
    mine = {"booking_id": "zz", "date": "2026-09-21", "time": "9:00 AM", "location": "Newtown", "name": "Other"}
    write(timetable / "pending_booking.json", mine)
    write(timetable / "standing_bookings.json", [rule()])
    assert standing.arm_standing_bookings() == 0
    assert len(queued(timetable)) == 3


def test_refuses_a_pending_file_it_does_not_understand(timetable):
    write(timetable / "pending_booking.json", "nope")
    write(timetable / "standing_bookings.json", [rule()])
    assert standing.arm_standing_bookings() == 1
