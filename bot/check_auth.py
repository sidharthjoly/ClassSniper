"""Prove the fast path can still sign in, without booking anything.

The venue rebuilt its auth in September 2026 and nothing announced it: the
browser fallback kept working, so every strike just quietly took twenty seconds
instead of milliseconds. The only way to notice was to look.

This signs in, asks whether the session took, and signs out again. It never
touches a booking endpoint. Run it by hand from the Actions tab whenever the
fast path looks suspicious, or after any change to the sign-in flow.

    python bot/check_auth.py
"""

import os
import sys

import requests

import striker

EMAIL = os.getenv("GYM_EMAIL")
PASSWORD = os.getenv("GYM_PASSWORD")


def check():
    if not EMAIL or not PASSWORD:
        print("GYM_EMAIL and GYM_PASSWORD must be set.")
        return 2

    session = requests.Session()

    before = session.get(f"{striker.API_BASE}/session", headers=striker.API_HEADERS, timeout=10)
    print(f"1. GET /session before sign-in -> {before.status_code} {before.text[:80]}")
    if before.status_code != 200:
        print("   The session endpoint is not answering; the fast path cannot work.")
        return 1

    login = session.post(striker.LOGIN_URL, json={"email": EMAIL, "password": PASSWORD},
                         headers=striker.API_HEADERS, timeout=10)
    # Never print the body: it is the one response that could echo an identifier.
    print(f"2. POST /login -> {login.status_code}")
    if login.status_code != 200:
        print(f"   Sign-in failed. Error code: {striker.redact(login.json().get('error', '?'))}")
        return 1

    cookies = list(session.cookies.keys())
    print(f"3. session cookie set -> {cookies or 'NONE'}")
    if not cookies:
        print("   Signed in but no cookie: the booking call would be rejected.")
        return 1

    after = session.get(f"{striker.API_BASE}/session", headers=striker.API_HEADERS, timeout=10)
    authenticated = after.status_code == 200 and after.json().get("authenticated") is True
    print(f"4. GET /session after sign-in -> authenticated={authenticated}")

    try:
        session.post(f"{striker.API_BASE}/logout", headers=striker.API_HEADERS, timeout=10)
        print("5. signed out again")
    except requests.RequestException:
        print("5. sign-out failed (harmless — the session expires on its own)")

    if authenticated:
        print("\nThe fast path can sign in. Booking itself is only exercised by a real strike.")
        return 0
    print("\nSigned in, but the session did not stick.")
    return 1


if __name__ == "__main__":
    sys.exit(check())
