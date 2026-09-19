import json
import sys
from datetime import datetime, timedelta

import pytz
import requests

CENTERS_URL = "https://cms.oneplayground.com.au/api/timetable/centers"
SESSIONS_URL = "https://cms.oneplayground.com.au/api/timetable/get-sessions-by-center-and-date"
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


def is_non_class(group_name):
    """Sauna/recovery bookings share this endpoint with actual classes."""
    name = (group_name or "").lower()
    return any(word in name for word in NON_CLASS_GROUP_KEYWORDS)


def load_existing():
    try:
        with open("class_list.json") as f:
            existing = json.load(f)
    except (OSError, ValueError):
        return []
    return existing if isinstance(existing, list) else []


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


def scrape():
    sydney_tz = pytz.timezone("Australia/Sydney")
    now = datetime.now(sydney_tz)
    today = now.date()
    from_date = today
    to_date = today + timedelta(days=LOOKAHEAD_DAYS + 1)  # API's to_date is exclusive
    min_start = now.replace(tzinfo=None) + timedelta(hours=MIN_LEAD_HOURS)

    centers = fetch_centers()
    classes = []

    for center in centers:
        try:
            sessions = fetch_sessions(center["id"], from_date, to_date)
        except requests.RequestException as e:
            print(f"Skipping {center.get('name')} ({center['id']}): {e}")
            continue

        for s in sessions:
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

    existing = load_existing()
    if existing and len(classes) < len(existing) * MIN_RETAINED_FRACTION:
        print(
            f"Refusing to overwrite class_list.json: scraped only {len(classes)} classes "
            f"against {len(existing)} already on file. Keeping the old list — check "
            "whether the sessions API changed shape or a fetch half-failed."
        )
        return 1

    with open("class_list.json", "w") as f:
        json.dump(classes, f, indent=2)

    print(f"Wrote {len(classes)} armable classes across {len(centers)} locations to class_list.json")
    return 0


if __name__ == "__main__":
    sys.exit(scrape())
