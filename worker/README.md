# ClassSniper API

The write path for the dashboard, so that using ClassSniper doesn't require
knowing what GitHub is.

## Why this exists

The dashboard used to call the GitHub Contents API directly from the browser,
which meant every user first had to create a fine-grained personal access token
and paste it in. That's a fine ask for the person who wrote the thing and an
impossible one for anyone else.

This Worker holds that token instead. The dashboard sends an access key it was
given once; everything GitHub-shaped stays on this side.

Reads didn't move — the repo is public, so the dashboard still fetches
`class_list.json` and friends straight from `raw.githubusercontent.com` with no
credential at all. Only writes come through here.

## What it doesn't do

It doesn't run the cron. The external trigger that wakes the booking workflow
every 60 seconds stays exactly where it is: it's the one part of this system with
a demonstrated cadence, Cloudflare's scheduler is best-effort in the same way
GitHub's is, and the end user never sees either. Moving it would risk strike
timing to change nothing anyone can see.

## Endpoints

All `POST`, all requiring `Authorization: Bearer <access key>`.

| Path | Body | Does |
|---|---|---|
| `/api/bookings` | the class | Arms it |
| `/api/bookings/remove` | `booking_id`, or date + time | Unarms it |
| `/api/rules` | the rule | Adds a standing rule |
| `/api/rules/remove` | `id`, or the rule's time | Removes one |

Each returns the full updated list, so the dashboard can re-render immediately
rather than waiting on the raw.githubusercontent.com CDN cache to expire.

## Setup

```bash
cd worker
npm install
```

**1. Create the GitHub token.** A [fine-grained token](https://github.com/settings/personal-access-tokens/new)
scoped to just this repo with **Contents: read & write**. Nothing else — this
Worker never touches Actions.

**2. Set both secrets.**

```bash
npx wrangler secret put GITHUB_TOKEN        # paste the token above
npx wrangler secret put DASHBOARD_PASSPHRASE # paste the generated access key
```

**3. Deploy.**

```bash
npx wrangler deploy
```

**4. Point the dashboard at it.** Put the deployed URL in `API_BASE` at the top
of `index.html`.

## Notes

- The access key is the only thing between the internet and your gym account, so
  it's generated with `crypto.getRandomValues` rather than chosen. It's compared
  in constant time, and the auth path is rate limited to 20 attempts a minute per
  IP — the hostname is public and guessable.
- CORS is locked to the dashboard's origin. A request from anywhere else is
  refused whether or not it has the key.
- Writes are read-modify-write against GitHub with a retry on conflict. Four
  things write `pending_booking.json` — this, the striker, the standing-rule
  matcher and the workflow's arm step — and the old browser-side version just
  surfaced a conflict as "failed".

```bash
npm test        # the auth gate
npm run dev     # local, against .dev.vars
npm run typecheck
```
