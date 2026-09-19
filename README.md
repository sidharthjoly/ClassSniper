# ClassSniper

The system automates gym class bookings. It strikes the moment the booking window opens.

**Live dashboard:** https://sidharthjoly.github.io/ClassSniper/

## The problem

The gym chain this targets ([One Playground](https://oneplayground.com.au)) opens bookings exactly 72 hours before class. Popular slots and instructors sell out instantly. You can't be expected to sit at a laptop and click buttons at the exact right second for multiple classes across multiple locations. It's not realistic. This automates the process.

## What it does

- An external cron service hits the `workflow_dispatch` endpoint every 60 seconds. It checks if a queued booking's 72-hour window is open. GitHub's `schedule:` trigger isn't precise enough. That's why we built this.
- When the window opens, the bot books the class. It uses a direct API call. If the path is inconclusive, it falls back to full browser automation. It's the only logical way to ensure success.
- A companion scraper pulls the live schedule for every studio location on the same 60-second cadence. The dashboard shows actual classes—name, instructor, spots remaining—instead of a blind date/time field. It's about data integrity.
- There is a hard safety rule. It blocks arming or striking anything less than **30 hours** away. Cancellations within 24 hours cost a fee. The tool won't generate a charge that isn't necessary.
- **Standing rules** handle the weekly case. The window is 72 hours wide. A weekly class requires a weekly arming. A rule describes it once. Every occurrence is queued as the scraper picks it up.
- **Losing the race isn't final.** A booking stays on a full class. It strikes the moment a spot reopens. Cancellations happen. The watch costs nothing because the scraper is already refreshing `remaining_spots` every 60s.

## Architecture

```
┌────────────────────┐
│  External cron     │  hits workflow_dispatch every 60s — see "Why not
│  (cron-job.org)    │  GitHub's own scheduler?" below
└─────────┬──────────┘
          ▼
┌──────────────────────────┐
│   Web dashboard          │  reads the JSON files directly — public, no auth
│   (GitHub Pages)         │  needed just to look
└─────────────┬────────────┘
              │ writes (arm / remove / add rule) go to…
              ▼
┌──────────────────────────┐
│   API worker + D1        │  holds what you booked, what's queued and your
│   (Cloudflare)           │  standing rules — the things that shouldn't be in
└─────────────┬────────────┘  a public repo. Reached with the access key.
              │ the bot pulls this state in before a run and pushes back
              │ what it changed
              ▼
┌──────────────────────────┐
│   This repo              │  source of truth: pending_booking.json,
│   (GitHub Actions)       │  standing_bookings.json, status.json,
└─────────────┬────────────┘  class_list.json, scrape_status.json
              │
     ┌────────┼─────────────────┐
     ▼        ▼                 ▼
 bot/        bot/           bot/striker.py
 scraper.py  standing.py    takes the next actionable booking; once its
 every       matches the    window opens:
 location's  rules against   1. fast path — two raw HTTP calls (login,
 live        the fresh          book), no browser
 schedule,   scrape and      2. falls back to full Playwright browser
 fetched     queues what        automation if that's inconclusive
 concurrently it finds
```

The three run in that order every tick. A class scraped this minute matches a rule. It gets struck in the same run.

## Key engineering details

- **Failures cannot be quiet.** That was the problem. We fixed it. `activity_type` vanished from the venue payload in September. The filter rejected every session. The scraper wrote an empty list. Because an empty picker looks like nothing happening, the dashboard stayed dead for four days. We have three layers now. The scraper asserts the payload has the fields the pipeline reads, including the two the *striker* posts to book. It refuses to overwrite a good class list with an empty or shrunken one. It writes a heartbeat on every run — including the runs that refuse — which the dashboard turns amber when it stops moving.
- **One lost class used to cost every booking behind it.** Selection took the earliest-opening eligible booking and returned after striking it. Only a win ever left the queue. A class that came back full was re-struck every 60 seconds for the ~42 hours until it hit the safety margin. Everything queued behind it never got a turn. It missed its window. An attempt now records its outcome on the booking. Resolved ones stay visible. They drop out of selection.
- **Class disambiguation.** Multiple distinct classes share the exact same time slot at the same location (e.g. 5:00 PM Newtown might be *Athletica*, *Mat Pilates*, and *Reformer: Strong* simultaneously). Matching by date + time alone is ambiguous. An early version of this bot got this wrong. Every class now carries a unique `booking_id` sourced from the venue's own session API. It is matched exactly. No guessing.
- **The fast path.** Found by reading the venue's own JS bundle instead of watching network traffic. The entire "click Book → sign in → confirm" flow the UI walks through is just two HTTP calls. Used directly. It is the same public API the site's own frontend calls. The full browser-automation flow stays as a tested, reliable fallback if the fast path fails.
- **Reading the bundle again when it broke.** The venue rebuilt auth in September 2026. `/person-auth` started 404ing. Sign-in moved to `/login`. The credential changed from a `personKey` in the booking payload to a session cookie. That payload is now keyed by `booking_id`. The fallback worked. Every strike worked. Nothing failed visibly. It just took twenty seconds instead of milliseconds. For a class that fills in seconds, that is the difference between success and failure. A good fallback hides the failure.
- **Timing.** The strike moment is precise. It is 72h before class start. The process sleeps until just before that. It warms the HTTP connection ~5 seconds ahead of time. This keeps the DNS/TLS handshake off the critical path. Then it fires. It records the milliseconds after the window opens that the request went out. That number is the point of the entire system. The ten locations are scraped concurrently for the same reason: that scrape took ~25 seconds, which was a third of the 60-second tick it shares with the strike.
- **Why not GitHub's own scheduler?** `schedule:` triggers on GitHub Actions are best-effort. They get deprioritized under load. This is a fact. In production, runs landed ~55-75 minutes apart despite a `*/5` cron. That is useless for "strike the instant it opens." An external cron service hitting `workflow_dispatch` every 60 seconds fixed the timing. It surfaced a second problem: overlapping runs racing each other on `git push`. I fixed this with a `concurrency` group (queue instead of running in parallel) plus a fetch-rebase retry loop around every push. A collision retries instead of failing.
- **Crash-safe cleanup.** Screenshots and result state are captured from inside the still-alive Playwright context. They are not captured after teardown. That was an easy mistake. The previous version swallowed real failures because `status.json` never recorded what went wrong.
- **Credentials never reach the public repo.** `status.json` is committed to a *public* repo. Playwright's own timeout errors embed the DOM element they were waiting on. This included the live `value` of the email input. It was the real login email in plaintext. I found this in an audit. I reproduced the leak. I fixed it at the source: the login-timeout handler now raises a clean message. I added two defense-in-depth layers. Debug screenshots mask any visible email/password field before capture (these are public Actions artifacts too). A final redaction pass scrubs exact credential matches from anything written to `status.json` regardless of source.
- **Respecting the platform.** The fast path makes at most 3 requests per attempt. It never retries. The login endpoint rate-limits at 5 requests. A fallback attempt needs some of that budget left for its own login. Repeated errors on one booking stop after 5 attempts. They do not retry for days. Watching a full class for a cancellation adds no requests. The scraper is already refreshing `remaining_spots` every 60 seconds. A freed spot is visible without asking again. A successful snipe costs two booking attempts — one to find it full, one to take the spot.

## Stack

Python. [Playwright](https://playwright.dev). GitHub Actions. vanilla HTML/CSS/JS
(no framework, no build step) on GitHub Pages.

## Tests

```
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests
```

They run against a trimmed recording of the real session payload. It is date-shifted at load. This ensures a fixture doesn't start passing for the wrong reason as it ages. It covers what actually went wrong. A vanished field. A half-failed scrape. A full class blocking the queue. A standing rule loose enough to arm a whole day's timetable. An auto-armed booking reappearing after you delete it. CI runs them on push and PR only. The booking workflow fires every 60 seconds. It has no business installing `pytest`.

## Setup (to adapt this for your own use)

1. Fork the repo.
2. Add repo secrets: `GYM_EMAIL`, `GYM_PASSWORD`.
3. Enable GitHub Pages: Settings → Pages → Source: **GitHub Actions**.
4. Enable Actions if the fork disabled them by default.
5. Deploy the API worker — see [worker/README.md](worker/README.md). It holds
   your bookings and serves the dashboard's writes. This is how people who don't
   know GitHub use the system. They get an access key. Not a personal access token.
   Put its URL in `API_BASE` at the top of
   `index.html`. Add `CLASSSNIPER_API` and `CLASSSNIPER_BOT_TOKEN` as repo
   secrets. The workflow needs them to reach it.
6. **Set up the external trigger** — don't skip this. The repo's own `schedule:`
   cron is a free backup. It isn't reliable enough to strike on time (see above).
   Create a second
   [fine-grained token](https://github.com/settings/personal-access-tokens/new)
   scoped to this repo with **Actions: read & write** only. Use a free
   service like [cron-job.org](https://cron-job.org) to `POST` every 60s to
   `https://api.github.com/repos/<you>/<repo>/actions/workflows/main.yml/dispatches`
   with header `Authorization: Bearer <token>` and body `{"ref":"main"}`.

## Who can actually use it

Anyone with the access key. You paste it once. The browser remembers it. There is no account. There is nothing to install.

Everything GitHub-shaped is behind [the worker](worker/README.md). That was deliberate. Setup used to begin with "create a fine-grained personal access token". That is a reasonable ask of the person who wrote this. It was the end of the conversation with anyone else.

## What's public and what isn't

The repo is public. The split is fundamental.

**Public, no key:** `class_list.json` and `scrape_status.json` — the gym's timetable and a heartbeat for the refresh status. Neither contains personal data. Browsing classes requires no key. It's that simple.

**Behind the key:** bookings, queues, and standing rules. These were `status.json`, `pending_booking.json` and `standing_bookings.json`. They exposed your physical location to anyone with access to the dashboard or the repo. A standing rule is the most granular version of that. It means "this person is at this gym at this time every Monday." They are in the worker's database now.

Old commits contain some past bookings. The history wasn't rewritten. Nothing new is published.

## Safety & scope

This automates one personal account. It handles booking actions for a service where the operator is a paying member. It uses the same public interfaces as the website. It doesn't bypass authentication. It doesn't access other members' data. It doesn't exceed the platform's documented rate limits.

## License

MIT — see [LICENSE](LICENSE).
