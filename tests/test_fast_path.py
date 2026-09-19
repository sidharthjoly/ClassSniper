"""The two HTTP calls that replace a 20-second browser run.

The venue rebuilt its auth in September 2026: /person-auth became /login, the
credential became a cookie instead of a personKey echoed back in the body, and
the booking payload's person_key became booking_id. None of that announced
itself — every strike just quietly took the slow path for weeks. These pin the
shapes so a repeat is a failing test rather than a slow strike.
"""

import asyncio
import json

import pytest

import striker


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = {} if body is None else body
        self.content = json.dumps(self._body).encode()
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSession:
    """Records what the fast path sends, and whether login left a cookie."""

    def __init__(self, login=None, sessions=None, book=None, sets_cookie=True):
        self.calls = []
        self.cookies = {}
        self._login = login or FakeResponse(200, {"expiresAt": "2026-09-20T00:00:00Z"})
        self._sessions = sessions
        self._book = book or FakeResponse(200, {"participation": {"participation_id": 4242}})
        self._sets_cookie = sets_cookie

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json))
        if url == striker.LOGIN_URL:
            if self._sets_cookie and self._login.status_code == 200:
                self.cookies = {"op_session": "abc"}
            return self._login
        if url == striker.SESSIONS_URL:
            return self._sessions
        if url == striker.BOOK_URL:
            return self._book
        raise AssertionError(f"unexpected url {url}")

    def sent_to(self, url):
        return next(payload for called, payload in self.calls if called == url)


TARGET = {"booking_id": "104book1", "date": "2026-09-25", "time": "6:00 AM", "location": "Newtown"}


def a_session_listing(**overrides):
    row = {"booking_id": "104book1", "pk": "RESOURCE#104br2#DATE#2026-09-25",
           "sk": "START#06:00#BOOKING#104book1", "booking_state": "ACTIVE", "remaining_spots": 5}
    row.update(overrides)
    return FakeResponse(200, {"sessions": [row]})


def strike(session):
    return asyncio.run(striker.try_fast_strike(TARGET, session))


def test_signs_in_at_the_login_endpoint():
    s = FakeSession(sessions=a_session_listing())
    strike(s)
    assert striker.LOGIN_URL.endswith("/login"), "auth moved off /person-auth"
    assert s.sent_to(striker.LOGIN_URL) == {"email": striker.EMAIL, "password": striker.PASSWORD}


def test_books_with_booking_id_not_a_person_key():
    """person_key was what the old API took; the server reads the member from
    the cookie now and the field became booking_id."""
    s = FakeSession(sessions=a_session_listing())
    status, _, _ = strike(s)
    assert status == "SUCCESS"
    payload = s.sent_to(striker.BOOK_URL)
    assert payload["booking_id"] == "104book1"
    assert "person_key" not in payload
    assert payload["pk"] and payload["sk"]


def test_a_login_that_leaves_no_cookie_is_an_error():
    """Being signed in *is* the cookie. Without it every later call 401s, so
    this fails here rather than three requests later."""
    s = FakeSession(sessions=a_session_listing(), sets_cookie=False)
    status, detail, _ = strike(s)
    assert status == "ERROR"
    assert "cookie" in detail.lower()


def test_a_rejected_password_is_reported_as_such():
    s = FakeSession(login=FakeResponse(401, {"error": "INVALID_CREDENTIALS"}), sessions=a_session_listing())
    status, detail, _ = strike(s)
    assert status == "ERROR"
    assert "INVALID_CREDENTIALS" in detail


def test_a_stale_cookie_is_told_apart_from_a_bad_booking():
    s = FakeSession(sessions=a_session_listing(),
                    book=FakeResponse(401, {"error": "SESSION_EXPIRED"}))
    status, detail, _ = strike(s)
    assert status == "ERROR"
    assert "session cookie was rejected" in detail


def test_the_participation_id_is_carried_into_the_status():
    """The only thing that can be checked against the account, on a path that
    has never been verified against a real booking."""
    s = FakeSession(sessions=a_session_listing())
    _, detail, _ = strike(s)
    assert "4242" in detail


def test_an_unrecognised_success_body_is_still_a_success():
    """Never inconclusive on a 200: the caller would send the browser flow in to
    book the same class a second time."""
    s = FakeSession(sessions=a_session_listing(), book=FakeResponse(200, {"ok": True}))
    status, detail, _ = strike(s)
    assert status == "SUCCESS"
    assert detail is None


def test_a_full_class_does_not_spend_a_booking_request():
    s = FakeSession(sessions=a_session_listing(remaining_spots=0))
    status, _, _ = strike(s)
    assert status == "FULL"
    assert all(url != striker.BOOK_URL for url, _ in s.calls)


def test_a_cancelled_session_is_not_booked():
    s = FakeSession(sessions=a_session_listing(booking_state="CANCELLED"))
    assert strike(s)[0] == "NOT_FOUND"


def test_at_most_three_requests_per_attempt():
    """The login endpoint rate-limits at 5, and a browser fallback needs some of
    that budget left for its own sign-in."""
    s = FakeSession(sessions=a_session_listing())
    strike(s)
    assert len(s.calls) <= 3


def test_a_booking_without_an_id_is_left_to_the_browser():
    s = FakeSession(sessions=a_session_listing())
    hand_entered = {"date": "2026-09-25", "time": "6:00 AM", "location": "Newtown"}
    assert asyncio.run(striker.try_fast_strike(hand_entered, s)) == (None, None, None)
    assert s.calls == []
