"""Where the venue's booking API lives, and how to talk to it.

Its own module so anything can import it without dragging in Playwright — the
sign-in check in particular, which has no business installing a browser to ask
whether a login still works.

Found by reading the site's own JS bundle. The venue rebuilt this in September
2026: /person-auth 404s now, sign-in is /login, and the credential is a session
COOKIE rather than a personKey handed back in the body. The frontend calls
everything with `credentials: "include"` and clears the old op_timetable_auth
localStorage entry on boot.
"""

TIMETABLE_URL = "https://oneplayground.com.au/classes/timetable/"

API_BASE = "https://cms.oneplayground.com.au/api/timetable"
LOGIN_URL = f"{API_BASE}/login"
LOGOUT_URL = f"{API_BASE}/logout"
SESSION_URL = f"{API_BASE}/session"
SESSIONS_URL = f"{API_BASE}/get-sessions-by-center-and-date"
BOOK_URL = f"{API_BASE}/create-participation-and-send-message"

# The endpoints are CORS-locked to the site's own origin and the session is a
# cookie, so present as what this is rather than as an anonymous script.
API_HEADERS = {
    "Origin": "https://oneplayground.com.au",
    "Referer": TIMETABLE_URL,
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
}

# What the frontend throws when the cookie is missing or stale — worth telling
# apart from a wrong password, which is not something a retry will fix.
SESSION_ERROR_CODES = ("SESSION_EXPIRED", "SESSION_REQUIRED")
