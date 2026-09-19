import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytz
import requests

from centers import CENTER_IDS

CENTERS_URL = "https://cms.oneplayground.com.au/api/timetable/centers"
SESSIONS_URL = "https://cms.oneplayground.com.au/api/timetable/get-sessions-by-center-and-date"
STATUS_FILE = "scrape_status.json"
CLASS_LIST_FILE = "class_list.json"
LOOKAHEAD_DAYS = 10

# Matches gym_script.py's MIN_LEAD_HOURS: cancelling inside 24h incurs a fee, so
# don't even offer a class that couldn't be armed anyway.
MIN_LEAD_HOURS = 30

# Every session used to carry activity_type: "CLASS_BOOKING", which is what told a
# real class apart from a recovery-suite booking. That field vanished from the API
# payload on 2026-09-15 — the equality check then rejected every session and the
# list silently emptied. The activity group is the field that still names those
# non-class bookings, so filter on that instead.
NON_CLASS_GROUP_KEYWORDS = ("sauna", "recovery", "plunge", "ice bath")

# An empty scrape, or one that loses most of the list, means something upstream
# broke — a dropped field, a half-finished fetch — not that the gym cancelled its
# whole timetable. Keep the last good list rather than blanking the dashboard.
MIN_RETAINED_FRACTION = 0.5

# What the rest of the pipeline reads off a session. activity_type used to be in
# this payload too; when it disappeared, every class vanished from the dashboard
# for four days because nothing was checking. Now something checks.
REQUIRED_SESSION_KEYS = (
    "booking_id",
    "booking_state",
    "booking_start_datetime",
    "activity_group_name",
    "activity_name",
    "center_name",
    "remaining_spots",
    "class_capacity",
    "instructors",
    # Not used here, but striker.py posts these two straight to the booking
    # endpoint — if they drift, the fast path breaks the same silent way.
    "pk",
    "sk",
)


def is_non_class(group_name):
    """Sauna/recovery bookings share this endpoint with actual classes."""
    name = (group_name or "").lower()
    return any(word in name for word in NON_CLASS_GROUP_KEYWORDS)


def missing_keys(session):
    return [key for key in REQUIRED_SESSION_KEYS if key not in session]


def unmapped_locations(classes):
    """Locations in the live timetable that striker.py has no center id for."""
    return sorted({c["location"] for c in classes if c["location"] not in CENTER_IDS})


def load_existing():
    try:
        with open(CLASS_LIST_FILE) as f:
            existing = json.load(f)
    except (OSError, ValueError):
        return []
    return existing if isinstance(existing, list) else []


def write_status(**fields):
    """Heartbeat for the dashboard. Written on every run, including the runs that
    refuse to touch class_list.json — a frozen pipeline is only visible if
    something keeps writing down the fact that it last ran."""
    with open(STATUS_FILE, "w") as f:
        json.dump(fields, f, indent=2)


def fetch_centers():
    response = requests.get(CENTERS_URL, timeout=20)
    response.raise_for_status()
    return response.json().get("centers", [])


def fetch_sessions(center_id, from_date, to_date):
    response = requests.post(
        SESSIONS_URL,
        json={
            "center_id": center_id,
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
        },
        timeout=20,
    )
    response.raise_for_status()
    return response.json().get("sessions", [])


def _fetch_one(center, from_date, to_date):
    try:
        return center, fetch_sessions(center["id"], from_date, to_date), None
    except requests.RequestException as e:
        return center, [], str(e)


def scrape():
    sydney_tz = pytz.timezone("Australia/Sydney")
    now = datetime.now(sydney_tz)
    today = now.date()
    from_date = today
    to_date = today + timedelta(days=LOOKAHEAD_DAYS + 1)  # API's to_date is exclusive
    min_start = now.replace(tzinfo=None) + timedelta(hours=MIN_LEAD_HOURS)

    centers = fetch_centers()

    # Ten sequential round-trips were eating ~25s of a 60s tick. Fetched together
    # they cost about as long as the slowest one. Results are re-ordered against
    # `centers` below, so the output stays stable run to run regardless of which
    # request finishes first.
    with ThreadPoolExecutor(max_workers=max(len(centers), 1)) as pool:
        results = list(pool.map(lambda c: _fetch_one(c, from_date, to_date), centers))

    classes = []
    failed_centers = []
    contract_breaks = {}

    for center, sessions, error in results:
        if error:
            print(f"Skipping {center.get('name')} ({center['id']}): {error}")
            failed_centers.append(center.get("name"))
            continue

        if sessions:
            gone = missing_keys(sessions[0])
            if gone:
                # Don't try to read a payload that has already been established as
                # not the payload this code was written against: dropping a key it
                # indexes by (booking_id, booking_start_datetime) raised straight
                # out of the loop, before anything recorded why.
                contract_breaks[center.get("name")] = gone
                continue

        for s in sessions:
            if not s.get("booking_id") or not s.get("booking_start_datetime"):
                continue
            if s.get("booking_state") != "ACTIVE":
                continue
            if is_non_class(s.get("activity_group_name")):
                continue
            start = datetime.strptime(s["booking_start_datetime"], "%Y-%m-%d %H:%M:%S")
            if start < min_start:
                continue
            classes.append({
                "booking_id": s["booking_id"],
                "date": start.strftime("%Y-%m-%d"),
                "time": start.strftime("%-I:%M %p"),
                "name": s.get("booking_name") or s.get("activity_name"),
                "class_type": s.get("activity_group_name"),
                "instructor": s.get("instructors"),
                "location": s.get("center_name") or center.get("name"),
                "remaining_spots": s.get("remaining_spots"),
                "capacity": s.get("class_capacity"),
            })

    classes.sort(key=lambda c: (c["date"], datetime.strptime(c["time"], "%I:%M %p"), c["location"]))

    unmapped = unmapped_locations(classes)
    status = {
        "scraped_at": now.isoformat(),
        "class_count": len(classes),
        "centers_total": len(centers),
        "centers_failed": failed_centers,
        "contract_breaks": contract_breaks,
        "unmapped_locations": unmapped,
    }

    if contract_breaks:
        for name, gone in contract_breaks.items():
            print(f"CONTRACT: {name} sessions are missing {', '.join(gone)}")
    if unmapped:
        print(f"CONTRACT: no center id in centers.py for {', '.join(unmapped)} — those would fall back to the browser path")

    existing = load_existing()
    refused = bool(existing) and len(classes) < len(existing) * MIN_RETAINED_FRACTION

    if refused:
        detail = (
            f"Refusing to overwrite class_list.json: scraped only {len(classes)} classes "
            f"against {len(existing)} already on file. Keeping the old list — check "
            "whether the sessions API changed shape or a fetch half-failed."
        )
        print(detail)
    else:
        # A missing field isn't worth blanking the dashboard over, so whatever did
        # parse still gets written — the run just doesn't get to call itself fine.
        with open(CLASS_LIST_FILE, "w") as f:
            json.dump(classes, f, indent=2)
        print(f"Wrote {len(classes)} armable classes across {len(centers)} locations to class_list.json")

    if contract_breaks or unmapped:
        write_status(result="contract_failed",
                     detail="Live payload no longer matches what this pipeline reads.", **status)
        return 2
    if refused:
        write_status(result="refused", detail=detail, **status)
        return 1

    write_status(result="ok", detail=None, **status)
    return 0


if __name__ == "__main__":
    sys.exit(scrape())
