"""Move the private state between the API worker and this run's working directory.

What you booked, what's queued and your standing rules used to be JSON files
committed to a public repo — which published a record of where you physically
are and when, to anyone who looked. They live in the worker's database now.

The bot scripts still read and write plain files, because that's what they're
tested against and the strike path is the last thing that should be learning to
speak HTTP. This pulls the files in before a run and pushes what changed back
after it.

  python bot/state.py pull
  python bot/state.py push

Push sends what the run *changed*, not what it ended up with. A run takes about
30 seconds on a 60 second tick, so someone arming a class from the dashboard is
very often doing it mid-run; writing back the whole list would drop their
booking about half the time, silently.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

PENDING_FILE = "pending_booking.json"
RULES_FILE = "standing_bookings.json"
STATUS_FILE = "status.json"

# What pull saw, so push can work out what this run actually changed. Local to
# the run; never committed.
SNAPSHOT_FILE = ".state_snapshot.json"

# Fields the striker writes onto a queued booking as it works.
RUN_WRITTEN_FIELDS = ("state", "attempts", "last_attempt")

ATTEMPTS = 3


def _config():
    base = os.environ.get("CLASSSNIPER_API", "").rstrip("/")
    token = os.environ.get("CLASSSNIPER_BOT_TOKEN", "")
    if not base or not token:
        raise SystemExit("CLASSSNIPER_API and CLASSSNIPER_BOT_TOKEN must both be set.")
    return base, token


def _call(method, path, payload=None):
    base, token = _config()
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{base}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            # Cloudflare's edge answers the default Python-urllib agent with a
            # 1010 before the request ever reaches the worker. Say who we are.
            "User-Agent": "classsniper-bot (+https://github.com/SidharthJoly/ClassSniper)",
        },
    )

    last = None
    for attempt in range(ATTEMPTS):
        if attempt:
            time.sleep(0.5 * 2 ** (attempt - 1))
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
            last = e
            print(f"state {method} {path} failed (attempt {attempt + 1}/{ATTEMPTS}): {e}")
    raise SystemExit(f"Could not reach the state API: {last}")


def same_booking(a, b):
    """Mirrors sameBooking in the worker: match on what doesn't move."""
    if a.get("booking_id") and b.get("booking_id"):
        return a["booking_id"] == b["booking_id"]
    return (
        a.get("date") == b.get("date")
        and a.get("time") == b.get("time")
        and (a.get("location") or "") == (b.get("location") or "")
        and (a.get("name") or "") == (b.get("name") or "")
    )


def same_rule(a, b):
    if a.get("id") and b.get("id"):
        return a["id"] == b["id"]
    return (
        a.get("weekday") == b.get("weekday")
        and a.get("time") == b.get("time")
        and (a.get("location") or "") == (b.get("location") or "")
    )


def fingerprint(booking):
    if booking.get("booking_id"):
        return {"booking_id": booking["booking_id"]}
    return {k: booking.get(k) for k in ("date", "time", "location", "name")}


def _read(path, fallback):
    try:
        with open(path) as f:
            value = json.load(f)
    except (OSError, ValueError):
        return fallback
    return value


def _write(path, value):
    with open(path, "w") as f:
        json.dump(value, f, indent=2)


def pull():
    """Fetch all three documents, or fail the run.

    Deliberately all-or-nothing and deliberately loud. The striker treats a
    missing queue file as "nothing to do" and exits green, so a half-finished
    pull would look exactly like an empty queue — a silently missed strike, on
    the one path that can't afford one. A failed run is recoverable; the next
    tick is 60 seconds away.
    """
    state = _call("GET", "/api/state")
    bookings = state.get("bookings")
    rules = state.get("rules")
    status = state.get("status")
    if not isinstance(bookings, list) or not isinstance(rules, list):
        raise SystemExit(f"State API returned something unexpected: {str(state)[:200]}")

    _write(PENDING_FILE, bookings)
    _write(RULES_FILE, rules)
    _write(STATUS_FILE, status if isinstance(status, dict) else {})
    _write(SNAPSHOT_FILE, {"bookings": bookings, "rules": rules, "status": status})

    print(f"Pulled {len(bookings)} queued booking(s) and {len(rules)} standing rule(s).")
    return 0


def pending_delta(before, after):
    """What the run did to the queue, as operations rather than a replacement."""
    delta = {}

    removed = [b for b in before if not any(same_booking(b, a) for a in after)]
    if removed:
        delta["remove"] = [fingerprint(b) for b in removed]

    added = [a for a in after if not any(same_booking(a, b) for b in before)]
    if added:
        delta["add"] = added

    updates = []
    for a in after:
        match = next((b for b in before if same_booking(a, b)), None)
        if match is None:
            continue
        fields = {k: a[k] for k in RUN_WRITTEN_FIELDS if k in a and a.get(k) != match.get(k)}
        if fields:
            updates.append({"match": fingerprint(a), "fields": fields})
    if updates:
        delta["update"] = updates

    return delta


def rules_delta(before, after):
    """Only ever the ids a rule has armed. The bot doesn't create or delete rules,
    so one deleted from the dashboard mid-run stays deleted."""
    armed = []
    for rule in after:
        match = next((r for r in before if same_rule(rule, r)), None)
        if match is None:
            continue
        if rule.get("armed_booking_ids") != match.get("armed_booking_ids"):
            armed.append({
                "match": {"id": rule["id"]} if rule.get("id") else {
                    k: rule.get(k) for k in ("weekday", "time", "location")
                },
                "armed_booking_ids": rule.get("armed_booking_ids", []),
            })
    return {"armed": armed} if armed else {}


def push():
    snapshot = _read(SNAPSHOT_FILE, None)
    if snapshot is None:
        raise SystemExit("No snapshot from this run's pull — refusing to guess at what changed.")

    body = {}

    pending = pending_delta(snapshot.get("bookings", []), _read(PENDING_FILE, []))
    if pending:
        body["pending"] = pending

    rules = rules_delta(snapshot.get("rules", []), _read(RULES_FILE, []))
    if rules:
        body["rules"] = rules

    status = _read(STATUS_FILE, {})
    if status and status != snapshot.get("status"):
        body["status"] = status

    if not body:
        print("Nothing changed this run.")
        return 0

    _call("POST", "/api/state/push", body)
    print(f"Pushed: {', '.join(sorted(body))}.")
    return 0


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "pull":
        sys.exit(pull())
    if command == "push":
        sys.exit(push())
    raise SystemExit("Usage: python bot/state.py [pull|push]")
