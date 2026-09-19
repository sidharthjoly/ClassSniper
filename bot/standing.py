"""Turn standing rules into armed bookings.

The booking window is 72 hours wide, so a class you take every week has to be
armed again every week — which means being at the dashboard, again, every week.
A standing rule describes the class once ("Tuesday 6:00 AM Reformer at
Newtown"); this runs after each scrape and queues anything new that matches.

Runs between the scraper and the striker, so a class matched this minute can be
struck in the same run.
"""

import json
import os
import sys
from datetime import datetime

CLASS_LIST_FILE = "class_list.json"
RULES_FILE = "standing_bookings.json"
PENDING_FILE = "pending_booking.json"

# Fields a rule can pin. Anything left unset matches everything.
MATCH_FIELDS = ("time", "location", "name", "class_type", "instructor")

# A standing rule means "this slot, every week". Without a time it means "every
# class at this location", which armed 58 bookings off one rule in testing — a
# whole day's timetable, twice over, every one of them a class you'd be booked
# into and charged for not attending. Cheap rule, expensive mistake.
REQUIRED_RULE_FIELDS = ("time",)

# Backstop for a rule specific enough to pass the check above but still broader
# than intended (a time with no weekday matches that slot every day). Arming stops
# at this many live bookings rather than filling the queue over successive runs.
MAX_PENDING = 10


def load_json(path, default):
    try:
        with open(path) as f:
            value = json.load(f)
    except (OSError, ValueError):
        return default
    return value


def write_json(path, value):
    """Write via a temp file and rename, so a reader never sees a half-written file.

    pending_booking.json has four writers now — the dashboard, the workflow's arm
    step, the striker and this — and the striker used to die on a malformed one
    before it could even record why.
    """
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(value, f, indent=2)
    os.replace(tmp, path)


def weekday_of(date_str):
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")


def matches(rule, cls):
    if rule.get("weekday") and rule["weekday"] != weekday_of(cls["date"]):
        return False
    for field in MATCH_FIELDS:
        wanted = rule.get(field)
        if wanted and wanted != cls.get(field):
            return False
    return True


def arm_standing_bookings():
    rules = load_json(RULES_FILE, [])
    if not isinstance(rules, list) or not rules:
        print("No standing rules to apply.")
        return 0

    classes = load_json(CLASS_LIST_FILE, [])
    if not classes:
        print("No class list to match against — skipping this run.")
        return 0

    pending = load_json(PENDING_FILE, [])
    if isinstance(pending, dict):
        pending = [pending]
    elif not isinstance(pending, list):
        print(f"{PENDING_FILE} is not a list — refusing to touch it.")
        return 1

    live_ids = {c["booking_id"] for c in classes}
    queued_ids = {b.get("booking_id") for b in pending}
    armed_count = 0

    for rule in rules:
        if not isinstance(rule, dict) or rule.get("enabled") is False:
            continue

        missing = [f for f in REQUIRED_RULE_FIELDS if not rule.get(f)]
        if missing:
            print(f"Ignoring rule {rule.get('id') or rule}: needs {', '.join(missing)} to be specific enough to arm.")
            continue

        # Every id this rule has ever armed, so removing an auto-armed booking from
        # the dashboard sticks. Without this the next run would just put it back —
        # deduping against the pending queue can't help, the entry is gone.
        already = rule.setdefault("armed_booking_ids", [])

        for cls in classes:
            if cls["booking_id"] in already or cls["booking_id"] in queued_ids:
                continue
            if not matches(rule, cls):
                continue
            if len(pending) >= MAX_PENDING:
                print(f"Queue is at {MAX_PENDING} bookings — not arming more. Clear some, or narrow the rule.")
                break

            pending.append({
                "booking_id": cls["booking_id"],
                "date": cls["date"],
                "time": cls["time"],
                "location": cls["location"],
                "name": cls["name"],
                "watch_if_full": bool(rule.get("watch_if_full")),
                "armed_by": rule.get("id") or rule.get("name") or "standing rule",
            })
            already.append(cls["booking_id"])
            queued_ids.add(cls["booking_id"])
            armed_count += 1
            print(f"Armed {cls['name']} at {cls['time']} on {cls['date']} ({cls['location']}) from a standing rule.")

        # Ids drop off the class list once the class is inside the 30h margin and
        # can never come back, so forgetting them here keeps the file from growing
        # forever without risking a re-arm.
        rule["armed_booking_ids"] = [i for i in already if i in live_ids]

    if armed_count:
        write_json(PENDING_FILE, pending)
    write_json(RULES_FILE, rules)

    print(f"Armed {armed_count} booking(s) from {len(rules)} standing rule(s).")
    return 0


if __name__ == "__main__":
    sys.exit(arm_standing_bookings())
