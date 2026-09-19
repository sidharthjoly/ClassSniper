# ClassSniper

Automated gym class booking that strikes the instant a class's booking window opens.

**Live dashboard:** https://sidharthjoly.github.io/ClassSniper/

## The problem

The gym chain this targets ([One Playground](https://oneplayground.com.au)) opens
bookings for each class exactly 72 hours before it starts. Popular time slots and
instructors can fill within moments of that window opening; remembering to be at
your laptop at the exact right second, for potentially several classes across
several locations, isn't realistic. This automates it.

## What it does

- An external cron service hits this repo's `workflow_dispatch` endpoint every
  60 seconds, which checks whether any queued booking's 72-hour window has
  opened yet. (Why not just GitHub's own `schedule:` trigger? See below.)
- When it has, the bot books the class — via a fast direct API call when possible,
  falling back to full browser automation if anything about that path is
  inconclusive.
- A companion scraper pulls the live class schedule across every studio location
  on the same cadence, so the web dashboard shows actual upcoming classes (name,
  instructor, spots remaining) to arm — not a blind date/time field.
- A hard safety rule blocks arming *or* striking anything starting less than
  **30 hours** away, since cancelling inside 24 hours incurs a fee. The tool
  won't let itself create a charge it didn't need to.
- **Standing rules** handle the weekly case: the window is only 72 hours wide, so
  a class you take every Monday has to be armed every Monday. A rule describes it
  once and each occurrence is queued as the scraper picks it up.
- **Losing the race isn't final.** A booking can be left watching a full class and
  strikes again the moment a spot reopens — cancellations are constant. The watch
  costs nothing: the scraper is already refreshing `remaining_spots` every 60s.

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
│   API worker             │  holds the GitHub token, so the dashboard only
│   (Cloudflare)           │  needs an access key. Read-modify-write with a
└─────────────┬────────────┘  retry on conflict; auth is rate limited
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

The three run in that order every tick, so a class scraped this minute can be
matched by a rule and struck in the same run.

## Key engineering details

- **Nothing is allowed to fail quietly.** This is the failure mode the project
  keeps hitting, so it's now designed against directly. `activity_type`
  disappeared from the venue's payload one day in September; the filter keying
  off it rejected every session, the scraper wrote an empty list, and because an
  empty picker looks exactly like a quiet afternoon, the dashboard sat dead for
  four days. Three layers now: the scraper asserts the payload still has the
  fields the pipeline reads (including the two the *striker* posts to book), it
  refuses to overwrite a good class list with an empty or drastically shrunken
  one, and it writes a heartbeat on every run — including the runs that refuse —
  which the dashboard turns amber when it stops moving.
- **One lost class used to cost every booking behind it.** Selection took the
  earliest-opening eligible booking and returned after striking it, and only a
  win ever left the queue — so a class that came back full was re-struck every
  60 seconds for the ~42 hours until it hit the safety margin, while everything
  queued behind it never got a turn and missed its own window. An attempt now
  records its outcome on the booking: resolved ones stay visible but drop out of
  selection.

- **Class disambiguation.** Multiple distinct classes routinely share the exact
  same time slot at the same location (e.g. 5:00 PM Newtown might be *Athletica*,
  *Mat Pilates*, and *Reformer: Strong* simultaneously). Matching by date + time
  alone is genuinely ambiguous — an early version of this bot got this wrong in
  practice. Every class now carries a unique `booking_id` sourced from the venue's
  own session API and matched exactly, not guessed.
- **The fast path.** Found by reading the venue's own JS bundle rather than just
  watching network traffic: the entire "click Book → sign in → confirm" flow the
  UI walks through turned out to be two HTTP calls under the hood. Used directly
  — the same public API the site's own frontend calls — with the full
  browser-automation flow kept as a tested, reliable fallback if anything about
  the fast path doesn't pan out.
- **Timing.** The strike moment is computed precisely (72h before class start).
  The process sleeps until just before it, warms up its HTTP connection ~5
  seconds ahead of time so the DNS/TLS handshake isn't sitting on the critical
  path, then fires — and records how many milliseconds after the window opening
  the request actually went out, since that number is the point of all of the
  above. The ten locations are scraped concurrently for the same reason: at ~25
  seconds, that scrape was eating a third of the 60-second tick it shares with
  the strike.
- **Why not GitHub's own scheduler?** `schedule:` triggers on GitHub Actions are
  documented as best-effort and get deprioritized under load — observed in
  production as runs landing ~55-75 minutes apart despite a `*/5` cron, which is
  useless for "strike the instant it opens." An external cron service hitting
  `workflow_dispatch` every 60 seconds instead fixed that, but surfaced a second
  problem: overlapping runs racing each other on `git push`. Fixed with a
  `concurrency` group (queue instead of running in parallel) plus a fetch-rebase
  retry loop around every push, so a collision retries instead of just failing.
- **Crash-safe cleanup.** Screenshots and result state are captured from inside
  the still-alive Playwright context, not after it's already torn down — an easy
  mistake that silently swallowed real failures in an earlier version, so
  `status.json` never actually recorded what had gone wrong.
- **Credentials never reach the public repo.** `status.json` is committed to a
  *public* repo, and Playwright's own timeout errors embed the DOM element they
  were waiting on — including the live `value` of the email input, i.e. the real
  login email in plaintext. Found in an audit, confirmed by reproducing the leak,
  fixed at the source (the login-timeout handler raises a clean message instead)
  plus two defense-in-depth layers: debug screenshots mask any visible
  email/password field before capture (they're public Actions artifacts too),
  and a final redaction pass scrubs exact credential matches from anything
  written to `status.json` regardless of source.
- **Respecting the platform.** The fast path makes at most 3 requests per attempt
  and never retries — the login endpoint rate-limits at 5 requests, and a
  fallback attempt needs some of that budget left for its own login. Repeated
  errors on one booking stop after 5 attempts rather than retrying for days, and
  watching a full class for a cancellation adds no requests at all: the scraper
  is already refreshing `remaining_spots` every 60 seconds, so a freed spot is
  visible without asking again. A successful snipe costs two booking attempts —
  one to find it full, one to take the spot.

## Stack

Python · [Playwright](https://playwright.dev) · GitHub Actions · vanilla HTML/CSS/JS
(no framework, no build step) on GitHub Pages.

## Tests

```
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests
```

They run against a trimmed recording of the real session payload, date-shifted
at load so a fixture can't start passing for the wrong reason as it ages, and
cover what has actually gone wrong here: a vanished field, a half-failed scrape,
a full class blocking the queue, a standing rule loose enough to arm a whole
day's timetable, an auto-armed booking reappearing after you delete it. CI runs
them on push and PR only — the booking workflow fires every 60 seconds and has
no business installing pytest.

## Setup (to adapt this for your own use)

1. Fork the repo.
2. Add repo secrets: `GYM_EMAIL`, `GYM_PASSWORD`.
3. Enable GitHub Pages: Settings → Pages → Source: **GitHub Actions**.
4. Enable Actions if the fork disabled them by default.
5. Deploy the API worker — see [worker/README.md](worker/README.md). It holds a
   [fine-grained token](https://github.com/settings/personal-access-tokens/new)
   (this repo, **Contents: read & write**) so the dashboard doesn't have to, and
   it's what makes the thing usable by someone who has never heard of GitHub:
   they get an access key, not a PAT. Put its URL in `API_BASE` at the top of
   `index.html`.
6. **Set up the external trigger** — don't skip this. The repo's own `schedule:`
   cron is kept as a free backup, but on its own it's not reliable enough to
   actually strike on time (see above). Create a second
   [fine-grained token](https://github.com/settings/personal-access-tokens/new)
   scoped to this repo with **Actions: read & write** only, then use a free
   service like [cron-job.org](https://cron-job.org) to `POST` every 60s to
   `https://api.github.com/repos/<you>/<repo>/actions/workflows/main.yml/dispatches`
   with header `Authorization: Bearer <token>` and body `{"ref":"main"}`.

## Who can actually use it

Anyone you give the access key to. Looking is free — status, the class list and
the queue are read straight from this public repo with no credential at all —
and arming needs the key, which is pasted once and remembered by the browser.

Everything GitHub-shaped lives behind [the worker](worker/README.md). That was
deliberate: setup used to begin "create a fine-grained personal access token",
which is a reasonable ask of the person who wrote this and the end of the
conversation with anyone else.

## Safety & scope

This automates a single personal account's own booking actions, against a
service the operator is an actual paying member of, using the same public
interfaces the service's own website already uses. It doesn't attempt to bypass
authentication, access other members' data, or exceed the platform's documented
rate limits.

## License

MIT — see [LICENSE](LICENSE).
