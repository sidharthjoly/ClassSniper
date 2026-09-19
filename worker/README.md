# ClassSniper API

The write path for the dashboard, so that using ClassSniper doesn't require
knowing what GitHub is.

## Why this exists

Two reasons, arrived at in that order.

**Nobody should need a GitHub account.** The dashboard used to call the GitHub
Contents API straight from the browser, so every user first had to create a
fine-grained personal access token and paste it in — a fine ask for the person
who wrote the thing and the end of the conversation with anyone else.

**And the personal state shouldn't have been public.** `status.json`,
`pending_booking.json` and `standing_bookings.json` were committed to a public
repo and read by the dashboard with no credential, which published which class
you booked, at which gym, at what time. They live in D1 here instead.

D1 rather than KV: KV is eventually consistent by up to 60 seconds, and a
booking armed from the dashboard has to be visible to the striker on its very
next run.

What stayed public is what isn't personal — `class_list.json` and
`scrape_status.json`, the gym's own timetable and a scrape heartbeat. The
dashboard still fetches those from `raw.githubusercontent.com` with no
credential, so browsing classes needs no key.

## What it doesn't do

It doesn't run the cron. The external trigger that wakes the booking workflow
every 60 seconds stays exactly where it is: it's the one part of this system with
a demonstrated cadence, Cloudflare's scheduler is best-effort in the same way
GitHub's is, and the end user never sees either. Moving it would risk strike
timing to change nothing anyone can see.

## Endpoints

All `POST`, all requiring `Authorization: Bearer <access key>`.

| Path | Method | Who | Does |
|---|---|---|---|
| `/api/state` | GET | either | Your status, queue and rules |
| `/api/bookings` | POST | either | Arms a class |
| `/api/bookings/remove` | POST | either | Unarms one |
| `/api/rules` | POST | either | Adds a standing rule |
| `/api/rules/remove` | POST | either | Removes one |
| `/api/state/push` | POST | bot only | What a run changed |

Two keys, not one. `DASHBOARD_PASSPHRASE` is handed to whoever uses the
dashboard; `BOT_TOKEN` is the workflow's, and only it may push a finished run.

Each returns the full updated list, so the dashboard can re-render immediately
rather than waiting on the raw.githubusercontent.com CDN cache to expire.

## Setup

```bash
cd worker
npm install
```

**1. Create the database and its table.**

```bash
npx wrangler d1 create classsniper-state    # put the id in wrangler.jsonc
npx wrangler d1 execute classsniper-state --remote --file=migrations/0001_state.sql
```

**2. Set both keys.** Generate them rather than choosing them — they're what
stands between the internet and a real gym account.

```bash
npx wrangler secret put DASHBOARD_PASSPHRASE  # what dashboard users are given
npx wrangler secret put BOT_TOKEN             # what the workflow uses
```

**3. Deploy.**

```bash
npx wrangler deploy
```

**4. Wire both ends.** Put the deployed URL in `API_BASE` at the top of
`index.html`, and add `CLASSSNIPER_API` and `CLASSSNIPER_BOT_TOKEN` as repo
secrets so the workflow can pull and push state.

This Worker holds no GitHub credential at all. Nothing in the system can write
to the repo except the workflow itself.

## Notes

- The access key is the only thing between the internet and your gym account, so
  it's generated with `crypto.getRandomValues` rather than chosen. It's compared
  in constant time, and the auth path is rate limited to 20 attempts a minute per
  IP — the hostname is public and guessable.
- CORS is locked to the dashboard's origin. A request from anywhere else is
  refused whether or not it has the key.
- Writes are read-modify-write with a version check and a retry. Both the
  dashboard and the bot write the queue, and a run overlaps the window in which
  someone is clicking.
- A finished run is pushed as what it *changed* — adds, removes, field updates —
  not what it ended up with. A run is ~30s of a 60s tick, so writing the whole
  list back would drop a booking armed mid-run about half the time. See
  `applyPendingDelta`.

```bash
npm test        # the auth gate
npm run dev     # local, against .dev.vars
npm run typecheck
```
