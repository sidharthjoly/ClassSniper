"""The scraper's job is to be right about what's armable, and loud when it can't be.

A field disappeared from the payload on 2026-09-15 and every class silently
vanished from the dashboard for four days. These are the nets for that.
"""

import json

import pytest

import scraper


@pytest.fixture
def live(monkeypatch, sessions):
    """Two centers, both returning the recorded payload, no network."""
    centers = [{"id": 104, "name": "Newtown"}, {"id": 105, "name": "Haymarket"}]
    monkeypatch.setattr(scraper, "fetch_centers", lambda: centers)
    monkeypatch.setattr(scraper, "fetch_sessions", lambda cid, f, t: sessions)
    return centers


def test_writes_the_armable_classes(workdir, live):
    assert scraper.scrape() == 0
    classes = json.loads((workdir / "class_list.json").read_text())
    assert classes
    assert all(c["booking_id"] and c["date"] and c["time"] for c in classes)


def test_leaves_out_what_cannot_be_booked(workdir, live, sessions):
    scraper.scrape()
    classes = json.loads((workdir / "class_list.json").read_text())
    kept = {c["booking_id"] for c in classes}

    for s in sessions:
        if s["booking_state"] != "ACTIVE":
            assert s["booking_id"] not in kept, f"{s['booking_state']} session was armable"
        if (s.get("activity_group_name") or "").lower().startswith("infrared sauna"):
            assert s["booking_id"] not in kept, "sauna slot was armable"


def test_creche_stays_armable(workdir, live, sessions):
    """It was bookable before the filter changed, and quietly dropping it would be a
    regression the class count is too coarse to notice."""
    creche = [s for s in sessions if s.get("activity_group_name") == "Creche" and s["booking_state"] == "ACTIVE"]
    if not creche:
        pytest.skip("no creche session in the fixture")
    scraper.scrape()
    kept = {c["booking_id"] for c in json.loads((workdir / "class_list.json").read_text())}
    assert creche[0]["booking_id"] in kept


# --- the contract check: this is the net that would have caught the outage ---

@pytest.mark.parametrize("dropped", scraper.REQUIRED_SESSION_KEYS)
def test_a_missing_field_is_reported_not_swallowed(workdir, monkeypatch, sessions, live, dropped):
    monkeypatch.setattr(scraper, "fetch_sessions",
                        lambda cid, f, t: [{k: v for k, v in s.items() if k != dropped} for s in sessions])
    assert scraper.scrape() == 2, f"dropping {dropped} passed silently"
    status = json.loads((workdir / "scrape_status.json").read_text())
    assert status["result"] == "contract_failed"
    assert dropped in next(iter(status["contract_breaks"].values()))


def test_a_location_with_no_center_id_is_reported(workdir, monkeypatch, live):
    monkeypatch.delitem(scraper.CENTER_IDS, "Newtown")
    assert scraper.scrape() == 2
    status = json.loads((workdir / "scrape_status.json").read_text())
    assert "Newtown" in status["unmapped_locations"]


def test_every_live_location_has_a_center_id(workdir, live):
    """Guards the striker's fast path: a location it has no id for falls back to a
    browser run, which is slower than the booking window is forgiving."""
    scraper.scrape()
    classes = json.loads((workdir / "class_list.json").read_text())
    assert scraper.unmapped_locations(classes) == []


# --- the write guard: a broken scrape must not blank the dashboard ---

def test_an_empty_scrape_keeps_the_last_good_list(workdir, monkeypatch, live):
    scraper.scrape()
    good = (workdir / "class_list.json").read_text()

    monkeypatch.setattr(scraper, "fetch_sessions", lambda cid, f, t: [])
    assert scraper.scrape() == 1
    assert (workdir / "class_list.json").read_text() == good
    assert json.loads((workdir / "scrape_status.json").read_text())["result"] == "refused"


def test_a_half_failed_scrape_keeps_the_last_good_list(workdir, monkeypatch, live, sessions):
    scraper.scrape()
    good = (workdir / "class_list.json").read_text()

    calls = {"n": 0}
    def flaky(cid, f, t):
        calls["n"] += 1
        if calls["n"] > 1:
            raise scraper.requests.RequestException("simulated 503")
        return sessions[:1]
    monkeypatch.setattr(scraper, "fetch_sessions", flaky)

    assert scraper.scrape() == 1
    assert (workdir / "class_list.json").read_text() == good


def test_the_guard_cannot_deadlock_on_an_empty_file(workdir, live):
    """Production sat at [] for four days. If the guard refused to write over an
    empty list, the fix for that outage could never have landed."""
    (workdir / "class_list.json").write_text("[]")
    assert scraper.scrape() == 0
    assert json.loads((workdir / "class_list.json").read_text())


def test_the_heartbeat_is_written_even_when_the_list_is_not(workdir, monkeypatch, live):
    scraper.scrape()
    monkeypatch.setattr(scraper, "fetch_sessions", lambda cid, f, t: [])
    scraper.scrape()
    status = json.loads((workdir / "scrape_status.json").read_text())
    assert status["scraped_at"] and status["result"] == "refused"
