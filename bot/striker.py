import os
import re
import json
import asyncio
from datetime import datetime, timedelta
from playwright.async_api import async_playwright
import pytz
import requests

# Center ids live in their own module so scraper.py can check the live timetable
# against them every run — a location that opens later can't quietly go missing
# here and send every booking for it down the slow browser path.
from centers import CENTER_IDS

# --- CONFIG FROM SECRETS ---
EMAIL = os.getenv("GYM_EMAIL")
PASSWORD = os.getenv("GYM_PASSWORD")


def redact(text):
    """Scrub the real credentials out of a string before it can reach status.json —
    a file that gets committed to a *public* repo. GitHub's automatic secret masking
    only covers CI log output, not file contents that get written and pushed, so this
    is the only thing standing between a leaky exception message and a permanent,
    public git-history record of the real email/password."""
    if not text:
        return text
    text = str(text)
    if EMAIL:
        text = text.replace(EMAIL, "[REDACTED]")
    if PASSWORD:
        text = text.replace(PASSWORD, "[REDACTED]")
    return text


def redact_value(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    return value


async def mask_sensitive_fields(page):
    """Best-effort: blank out any visible email/password field before a screenshot.
    Password fields already render masked in-browser, but email doesn't — a debug
    screenshot of a stuck login modal would otherwise show the real address in the
    clear, and workflow artifacts on a public repo are downloadable by anyone signed
    into GitHub, not just people with write access to this repo."""
    try:
        await page.evaluate("""
            document.querySelectorAll('input[type="email"], input[type="password"]').forEach(el => {
                if (el.value) el.value = '\\u2022'.repeat(8);
            });
        """)
    except Exception:
        pass

TIMETABLE_URL = "https://oneplayground.com.au/classes/timetable/"
DEFAULT_LOCATION = "Newtown"

# Outcomes that settle a queued booking for good. It stays in pending_booking.json
# so the dashboard can say what happened, but is never struck at again.
TERMINAL_STATES = ("full", "failed")

CLASS_LIST_FILE = "class_list.json"


def free_spots(booking_id):
    """Spots left on a class according to the last scrape, or None if it isn't there.

    Watching a full class for a cancellation costs nothing extra: the scraper
    already refreshes remaining_spots for every class every 60s, so the freed spot
    is visible here without spending a single request against a booking API this
    project is deliberately light on.
    """
    try:
        with open(CLASS_LIST_FILE) as f:
            classes = json.load(f)
    except (OSError, ValueError):
        return None
    for c in classes:
        if c.get("booking_id") == booking_id:
            return c.get("remaining_spots") or 0
    return None

# --- Fast API path ---
# Found by reading the site's own JS bundle: the whole booking flow is two HTTP
# calls. No browser needed at all if this works.
#
# The venue rebuilt its auth in September 2026 and /person-auth now 404s, which
# had every strike silently falling back to the ~20s browser flow. Read the
# bundle again: sign-in is /login and the session is a COOKIE now, not a
# personKey handed back in the body to pass along in the booking payload. The
# frontend calls everything with `credentials: "include"` and clears the old
# localStorage token on boot. requests.Session carries the cookie for us.
API_BASE = "https://cms.oneplayground.com.au/api/timetable"
LOGIN_URL = f"{API_BASE}/login"
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

def warm_connection(session):
    """Best-effort: establish the TCP/TLS connection to the API host ahead of time so
    the DNS lookup + handshake (routinely 100-300ms+) doesn't sit on the critical path
    of the actual strike. A failure here just forfeits the warm-up benefit — never fatal.
    """
    try:
        session.get(f"{API_BASE}/centers", timeout=5)
    except Exception:
        pass


def _participation_id(body):
    """Pull the id the venue assigns a booking, when it's in the response.

    Not used to decide success — only to put something in status.json that can
    be checked against the account, since this path has never been verified
    against a real booking.
    """
    try:
        participation = body.get("participation") or {}
        if participation.get("participation_id"):
            return f"Booked via the fast path, participation {participation['participation_id']}."
        first = ((body.get("response") or {}).get("participations") or [{}])[0]
        if first.get("participationId"):
            return f"Booked via the fast path, participation {first['participationId']}."
    except (AttributeError, IndexError, TypeError):
        pass
    return None


async def try_fast_strike(target, session):
    """Attempt the whole strike as two raw HTTP calls instead of driving a browser.

    Returns (status, detail, raw_response):
      status is None            -> not attempted (no booking_id/unknown location); caller
                                    should fall back to the Playwright flow silently.
      status is "SUCCESS"       -> booked. Unverified against a real account — no test
                                    credentials available — so callers should still treat
                                    the first few real successes as needing a sanity check.
      status is "FULL"          -> definitively full, no point falling back to Playwright.
      status is "NOT_FOUND"/"ERROR" -> inconclusive; caller should fall back to Playwright.

    Deliberately makes at most 3 requests (login + session-refresh in parallel, then
    book) and never retries — the login endpoint rate-limits at 5 requests, and a
    Playwright fallback needs some of that budget left for its own login attempt.

    `session` should already be warmed up (see warm_connection) — reusing its
    connection pool means the login/refresh/book calls skip repeating the TCP/TLS
    handshake against the same host.
    """
    booking_id = target.get("booking_id")
    location = target.get("location", DEFAULT_LOCATION)
    center_id = CENTER_IDS.get(location)
    if not booking_id or not center_id:
        return None, None, None

    def _login():
        # Sets the session cookie on `session`; the body only carries expiresAt.
        return session.post(LOGIN_URL, json={"email": EMAIL, "password": PASSWORD},
                            headers=API_HEADERS, timeout=10)

    def _refresh_session():
        to_date = (datetime.strptime(target["date"], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        return session.post(SESSIONS_URL, json={
            "center_id": center_id, "from_date": target["date"], "to_date": to_date,
        }, headers=API_HEADERS, timeout=10)

    try:
        login_resp, session_resp = await asyncio.gather(
            asyncio.to_thread(_login), asyncio.to_thread(_refresh_session),
        )
    except Exception as e:
        return "ERROR", f"Fast-path network error: {e}", None

    if login_resp.status_code != 200:
        return "ERROR", f"Fast-path login failed: {login_resp.status_code} {login_resp.text[:200]}", None
    # No token to read any more: being signed in *is* the cookie now. If it
    # didn't arrive, every later call would 401 on an expired-session code, so
    # say so here rather than three requests later.
    if not session.cookies:
        return "ERROR", "Fast-path login returned no session cookie", None

    if session_resp.status_code != 200:
        return "ERROR", f"Fast-path session refresh failed: {session_resp.status_code}", None
    sessions = session_resp.json().get("sessions", [])
    match = next((s for s in sessions if s.get("booking_id") == booking_id), None)
    if not match:
        return "NOT_FOUND", "Fast-path: booking_id no longer present in session list", None
    if match.get("booking_state") != "ACTIVE":
        return "NOT_FOUND", f"Fast-path: session state is {match.get('booking_state')}", None
    if (match.get("remaining_spots") or 0) <= 0:
        return "FULL", "Fast-path: class is full", None

    def _book():
        # booking_id replaced person_key here in the same rebuild: the server
        # takes the member from the cookie, not the payload.
        return session.post(BOOK_URL, json={
            "pk": match["pk"], "sk": match["sk"], "booking_id": booking_id,
            "send_confirmation_message": True,
        }, headers=API_HEADERS, timeout=10)

    try:
        book_resp = await asyncio.to_thread(_book)
    except Exception as e:
        return "ERROR", f"Fast-path booking request failed: {e}", None

    if book_resp.status_code == 200:
        # A 200 is taken as booked. Deliberately not stricter: treating a 200
        # whose shape we don't recognise as inconclusive would send the browser
        # fallback in to book the same class a second time.
        body = book_resp.json() if book_resp.content else {}
        return "SUCCESS", _participation_id(body), body

    detail = book_resp.text[:300]
    if book_resp.status_code == 401 and any(c in detail for c in SESSION_ERROR_CODES):
        return "ERROR", f"Fast-path: the session cookie was rejected ({detail})", None
    return "ERROR", f"Fast-path booking failed: {book_resp.status_code} {detail}", None


# Matches both "6:00 AM" (pending_booking.json) and the site's own compact
# "6AM" / "6.30AM" class-list format, so the two can be compared directly.
TIME_RE = re.compile(r'^(\d{1,2})(?:[.:](\d{2}))?\s*([AaPp][Mm])$')


def parse_class_time(text):
    m = TIME_RE.match(text.strip())
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    ampm = m.group(3).upper()
    return (hour, minute, ampm)


async def select_location(page, location_name):
    await page.click('[data-testid="location-filter"]')
    await page.get_by_role("button", name=location_name, exact=True).click()
    await page.wait_for_selector('[data-testid="day-tab"]', timeout=15000)


async def select_day(page, target_date):
    """Click the day-tab matching target_date, paging forward through weeks if needed."""
    target_day_num = str(target_date.day)
    expected_header = target_date.strftime("%A, %b %-d").upper()

    for _ in range(8):  # generous cap; target is normally within the first window
        tabs = page.locator('[data-testid="day-tab"]')
        count = await tabs.count()
        for i in range(count):
            tab = tabs.nth(i)
            if await tab.is_disabled():
                continue
            text = (await tab.inner_text()).strip()
            day_num = text.splitlines()[-1].strip()
            if day_num == target_day_num:
                await tab.click()
                await page.wait_for_timeout(700)
                if await page.locator("h3", has_text=expected_header).count() > 0:
                    return True
                break  # day-number matched but wrong month/header; keep paging
        next_btn = page.locator('button[aria-label="Next week"]')
        if await next_btn.is_disabled():
            break
        await next_btn.click()
        await page.wait_for_timeout(700)
    return False


async def select_ampm(page, ampm):
    """The AM/PM toggle actually filters which classes render — defaults to AM only."""
    await page.get_by_role("button", name=ampm, exact=True).click()
    await page.wait_for_timeout(500)


async def ensure_logged_in(page):
    """The site only prompts for login when it's actually needed (e.g. on Book click)."""
    email_input = page.locator('input[type="email"]')
    if await email_input.is_visible():
        await email_input.fill(EMAIL)
        await page.locator('input[type="password"]').fill(PASSWORD)
        await page.click('[data-testid="login-submit"]')
        try:
            await email_input.wait_for(state="hidden", timeout=15000)
        except Exception:
            # Playwright's own timeout error embeds the resolved element's live DOM,
            # including this input's value attribute — i.e. the real email address in
            # the clear. Never let that raw message propagate to status.json.
            raise RuntimeError(
                "Login did not complete within 15s (wrong credentials, or the site's login flow changed)."
            )


async def _click_row_book_button(row):
    # The site renders both a desktop and mobile layout for each row (only one
    # actually visible at a time), each with its own book-btn — scope to the
    # visible one to avoid a strict-mode "resolved to 2 elements" error.
    book_btn = row.locator('[data-testid="book-btn"]:visible')
    if await book_btn.count() == 0:
        return "NOT_FOUND", "Matched the class row but found no visible book button."
    if not await book_btn.is_enabled():
        btn_text = (await book_btn.inner_text()).strip()
        return "FULL", f"Class is full ({btn_text})."
    await book_btn.click()
    return "CLICKED", None


async def find_and_click_book(page, target):
    """Returns (status, detail) where status is one of CLICKED / FULL / NOT_FOUND.

    Same-time slots at the same location routinely have multiple distinct classes
    (e.g. "Reformer: Sculpt" and "Athletica" both at 6:00 AM), so time alone can't
    tell them apart. Prefer the exact booking_id from class_list.json when present;
    fall back to time-matching for manually-typed entries that don't have one.
    """
    booking_id = target.get("booking_id")
    if booking_id:
        row = page.locator(f'[data-booking-id="{booking_id}"] [data-testid="class-row"]')
        if await row.count() > 0:
            return await _click_row_book_button(row)
        # booking_id not on the page (e.g. stale class_list.json) — fall through
        # to time-matching rather than failing outright.

    target_parsed = parse_class_time(target["time"])
    rows = page.locator('[data-testid="class-row"]')
    count = await rows.count()
    for i in range(count):
        row = rows.nth(i)
        time_el = row.locator('[data-testid="session-start-time"]')
        if await time_el.count() == 0:
            continue
        row_parsed = parse_class_time((await time_el.inner_text()).strip())
        if row_parsed != target_parsed:
            continue
        return await _click_row_book_button(row)
    return "NOT_FOUND", f"No class row found for time {target['time']}."


async def _run_strike(page, target, location_name, result):
    """Drives the actual browser interaction. Mutates `result` in place; raises on
    any unexpected error so the caller can screenshot before the driver tears down."""
    print(f"Opening timetable and selecting {location_name}...")
    await page.goto(TIMETABLE_URL, wait_until="networkidle", timeout=30000)
    await page.wait_for_selector('[data-testid="location-filter"]', timeout=15000)
    await select_location(page, location_name)

    print(f"Selecting {target['date']}...")
    target_date_obj = datetime.strptime(target['date'], "%Y-%m-%d")
    if not await select_day(page, target_date_obj):
        await mask_sensitive_fields(page)
        await page.screenshot(path="final_failure.png")
        result.update({
            "status": "NOT_FOUND",
            "time": str(datetime.now()),
            "error": f"Could not find {target['date']} in the day picker.",
        })
        return

    target_parsed_time = parse_class_time(target['time'])
    if target_parsed_time is None:
        result.update({
            "status": "ERROR",
            "time": str(datetime.now()),
            "error": f"Could not parse time '{target['time']}' (expected e.g. '6:00 AM').",
        })
        return
    await select_ampm(page, target_parsed_time[2])

    book_status = None
    book_detail = None
    for attempt in range(1, 6):
        book_status, book_detail = await find_and_click_book(page, target)
        if book_status == "CLICKED":
            print(f"Attempt {attempt}: found {target['time']} and clicked Book.")
            break
        print(f"Attempt {attempt}: {book_detail}")
        if book_status == "FULL":
            break
        await asyncio.sleep(4)

    if book_status == "FULL":
        result.update({
            "status": "FAILED",
            "reason": "FULL",
            "time": str(datetime.now()),
            "error": book_detail,
        })
    elif book_status != "CLICKED":
        await mask_sensitive_fields(page)
        await page.screenshot(path="final_failure.png")
        result.update({
            "status": "NOT_FOUND",
            "time": str(datetime.now()),
            "error": book_detail,
        })
    else:
        # Site only asks to sign in once you try to actually book.
        await page.wait_for_timeout(1000)
        await ensure_logged_in(page)
        await page.wait_for_timeout(1500)

        confirm_btn = page.get_by_role("button", name=re.compile("confirm", re.I))
        if await confirm_btn.count() > 0:
            await confirm_btn.first.click()
            await page.wait_for_timeout(1000)

        # The post-login/confirm UI hasn't been verified end-to-end against a real
        # account yet, so capture a screenshot every time and flag it for review
        # rather than assuming the booking definitely went through.
        await mask_sensitive_fields(page)
        await page.screenshot(path="booking_result.png")
        result.update({
            "status": "SUCCESS",
            "time": str(datetime.now()),
            "note": "Clicked Book and completed the sign-in/confirm flow — verify against booking_result.png artifact until this path is confirmed reliable.",
        })


async def run_booking():
    # 1. Load your pending bookings from the repository
    try:
        with open('pending_booking.json', 'r') as f:
            data = json.load(f)
    except FileNotFoundError:
        print("No pending_booking.json found. Create one to start.")
        return
    except ValueError as e:
        # Several things write this file — the dashboard, the workflow's arm step,
        # this script. A half-written one used to raise here and kill the run before
        # status.json was written, which is the same silent-failure shape that has
        # bitten this project before. Say so in the status file instead.
        print(f"pending_booking.json is not valid JSON: {e}")
        with open('status.json', 'w') as f:
            json.dump({
                "status": "ERROR",
                "time": str(datetime.now()),
                "error": f"pending_booking.json could not be parsed: {e}",
            }, f)
        return

    if isinstance(data, dict):
        targets = [data]
    elif isinstance(data, list):
        targets = data
    else:
        raise ValueError("pending_booking.json must contain an object or an array of objects")

    # Define Sydney Timezone
    sydney_tz = pytz.timezone('Australia/Sydney')

    # Cancelling inside 24h of a class incurs a charge. Never strike a class that
    # starts less than this many hours from now, so it can always be safely cancelled.
    MIN_LEAD_HOURS = 30

    # A booking that keeps erroring shouldn't keep trying for days. The login
    # endpoint rate-limits at 5 requests and every attempt spends one.
    MAX_ATTEMPTS = 5

    now = datetime.now(sydney_tz)

    bookings = []
    for target in targets:
        target_dt = sydney_tz.localize(datetime.strptime(f"{target['date']} {target['time']}", "%Y-%m-%d %I:%M %p"))
        bookings.append({
            "target": target,
            "target_dt": target_dt,
            "opens_at": target_dt - timedelta(hours=72),
        })

    if not bookings:
        print("No pending bookings queued. Nothing to do.")
        return

    eligible = [b for b in bookings if (b["target_dt"] - now) >= timedelta(hours=MIN_LEAD_HOURS)]
    for b in bookings:
        if b not in eligible:
            print(
                f"Skipping {b['target']}: class starts in {b['target_dt'] - now}, "
                f"under the {MIN_LEAD_HOURS}h cancellation-safety margin."
            )

    if not eligible:
        result = {
            "status": "BLOCKED_TOO_CLOSE",
            "time": str(datetime.now()),
            "error": f"All pending bookings start within {MIN_LEAD_HOURS}h; skipped to avoid a cancellation-fee risk.",
        }
        with open('status.json', 'w') as f:
            json.dump(result, f)
        return

    # Selection used to be "earliest-opening eligible booking", full stop, and only a
    # SUCCESS ever left the queue. A class that came back FULL was therefore re-struck
    # every 60s until it fell under the 30h margin — ~42 hours of retries for one
    # class — and since a run returns after striking whatever it selected, every
    # booking queued behind it missed its own window entirely. That defeats the one
    # thing this project exists to do. Resolved bookings stay in the file for the
    # dashboard to explain, but drop out of selection.
    def strikeable(t):
        state = t.get("state")
        if state in TERMINAL_STATES:
            return False
        if state == "watching":
            # Full last time, and flagged to keep watching for a cancellation. The
            # scrape is the source of truth for whether a spot came back.
            return (free_spots(t.get("booking_id")) or 0) > 0
        return True

    actionable = [b for b in eligible if strikeable(b["target"])]
    for b in eligible:
        if b not in actionable:
            t = b["target"]
            state = t.get("state")
            if state == "watching":
                print(f"Watching {t.get('name') or t['time']} on {t['date']}: still full, nothing to strike.")
            else:
                print(f"Skipping {t.get('name') or t['time']} on {t['date']}: already resolved ({state}).")

    if not actionable:
        # Deliberately leaves status.json alone: the run that resolved these already
        # recorded what happened, and overwriting it every 60s would bury it.
        if any(b["target"].get("state") == "watching" for b in eligible):
            print("Nothing to strike this run — watched bookings are still full.")
        else:
            print("Every queued booking has already resolved. Remove them from the dashboard to clear the queue.")
        return

    actionable.sort(key=lambda entry: entry["opens_at"])
    selected = actionable[0]
    target = selected["target"]
    booking_opens_at = selected["opens_at"]

    window = timedelta(minutes=15)

    if now < booking_opens_at - window:
        print(
            f"Too early for the next pending booking. Earliest open time is {booking_opens_at}, current Sydney time is {now}."
        )
        return

    strike_at = booking_opens_at + timedelta(seconds=2)
    warm_up_lead = timedelta(seconds=5)
    session = requests.Session()

    warm_up_at = strike_at - warm_up_lead
    if now < warm_up_at:
        wait_seconds = (warm_up_at - now).total_seconds()
        print(f"Booking opens at {booking_opens_at}. Waiting {wait_seconds:.1f}s, then warming up the connection before striking at {strike_at}.")
        await asyncio.sleep(wait_seconds)

    print("Warming up connection to the booking API...")
    await asyncio.to_thread(warm_connection, session)

    now = datetime.now(sydney_tz)
    if now < strike_at:
        sleep_seconds = (strike_at - now).total_seconds()
        print(f"Sleeping {sleep_seconds:.2f}s more to hit the exact opening moment...")
        await asyncio.sleep(sleep_seconds)

    result = {
        "status": "PENDING",
        "time": str(datetime.now()),
        "selected_target": target,
        "selected_open_time": str(booking_opens_at),
    }

    location_name = target.get("location", DEFAULT_LOCATION)

    # The whole design here — warming the connection, sleeping to the exact second —
    # is about how close to the window opening the first request actually lands, and
    # nothing was writing that number down. booking_opens_at is Sydney-aware, so this
    # has to be too or the subtraction raises.
    fired_at = datetime.now(sydney_tz)
    result["strike_latency_ms"] = round((fired_at - booking_opens_at).total_seconds() * 1000)

    try:
        print("Trying the fast API path first (no browser)...")
        fast_status, fast_detail, fast_raw = await try_fast_strike(target, session)

        if fast_status == "SUCCESS":
            print("Fast path booked it.")
            result.update({
                "status": "SUCCESS",
                "path": "fast",
                "time": str(datetime.now()),
                # fast_detail carries the venue's participation id when the
                # response included one — the one thing that can be checked
                # against the account, on a path never verified against a real
                # booking.
                "note": fast_detail or "Booked via the fast API path (no browser) — this path is "
                        "unverified against a real account, so treat an early SUCCESS here as "
                        "needing a sanity check against what actually happened.",
            })
        elif fast_status == "FULL":
            print(f"Fast path: {fast_detail}")
            result.update({
                "status": "FAILED",
                "reason": "FULL",
                "path": "fast",
                "time": str(datetime.now()),
                "error": fast_detail,
            })
        else:
            result["path"] = "browser"
            if fast_status is not None:
                print(f"Fast path inconclusive ({fast_status}: {fast_detail}). Falling back to the browser flow...")
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                try:
                    await _run_strike(page, target, location_name, result)
                except Exception:
                    # Screenshot here, while the Playwright driver is still alive — taking
                    # it from the outer except (after `async with` has torn down) silently
                    # fails, which is exactly the bug that hid earlier production failures.
                    try:
                        await mask_sensitive_fields(page)
                        await page.screenshot(path="final_failure.png")
                    except Exception:
                        pass
                    raise
    except Exception as e:
        print(f"Strike failed: {e}")
        result.update({
            "status": "ERROR",
            "time": str(datetime.now()),
            "error": str(e),
        })
    finally:
        result["strike_took_ms"] = round((datetime.now(sydney_tz) - fired_at).total_seconds() * 1000)

        if result.get("status") == "SUCCESS":
            try:
                targets.remove(target)
            except ValueError:
                pass
        else:
            target["last_attempt"] = str(datetime.now())
            if result.get("reason") == "FULL":
                # A full class isn't an error and shouldn't spend the error budget.
                # Cancellations happen constantly, so with watch_if_full set the
                # booking stays live and gets re-struck the moment a scrape shows a
                # free spot. That needs a booking_id to match on, so anything armed
                # by hand without one just resolves.
                watchable = target.get("watch_if_full") and target.get("booking_id")
                target["state"] = "watching" if watchable else "full"
            else:
                target["attempts"] = target.get("attempts", 0) + 1
                if target["attempts"] >= MAX_ATTEMPTS:
                    target["state"] = "failed"

        with open('pending_booking.json', 'w') as f:
            json.dump(targets, f, indent=2)

        with open('status.json', 'w') as f:
            json.dump(redact_value(result), f)

        if 'browser' in locals():
            try:
                await browser.close()
            except Exception:
                pass

if __name__ == "__main__":
    asyncio.run(run_booking())
