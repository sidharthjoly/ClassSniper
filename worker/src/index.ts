/**
 * The write path for the ClassSniper dashboard.
 *
 * The dashboard used to talk to the GitHub Contents API straight from the browser,
 * which meant anyone using it first had to create a fine-grained personal access
 * token — a sentence that ends the conversation with anyone who doesn't already
 * know what GitHub is. This holds that token instead, so the dashboard only ever
 * needs a passphrase.
 *
 * Reads still go to raw.githubusercontent.com directly: the repo is public, they
 * need no credential, and routing them through here would only add a hop.
 */

import { ConflictError, mutate, type RepoConfig } from "./github";

const PENDING_FILE = "pending_booking.json";
const RULES_FILE = "standing_bookings.json";

interface Booking {
  booking_id?: string;
  date: string;
  time: string;
  location?: string;
  name?: string;
  watch_if_full?: boolean;
  [key: string]: unknown;
}

interface Rule {
  id?: string;
  weekday?: string;
  time: string;
  location?: string;
  name?: string;
  enabled?: boolean;
  watch_if_full?: boolean;
  armed_booking_ids?: string[];
}

class BadRequest extends Error {}

function repoConfig(env: Env): RepoConfig {
  return {
    owner: env.GITHUB_OWNER,
    repo: env.GITHUB_REPO,
    branch: env.GITHUB_BRANCH,
    token: env.GITHUB_TOKEN,
  };
}

/**
 * Same booking, by the fields that don't move.
 *
 * Deliberately ignores state/attempts/last_attempt: the striker writes those
 * between the dashboard loading the list and someone clicking remove, and
 * comparing whole objects would fail to match exactly when a booking has just
 * resolved — which is the moment people most want to clear it.
 */
function sameBooking(a: Partial<Booking>, b: Partial<Booking>): boolean {
  if (a.booking_id && b.booking_id) return a.booking_id === b.booking_id;
  return (
    a.date === b.date &&
    a.time === b.time &&
    (a.location ?? "") === (b.location ?? "") &&
    (a.name ?? "") === (b.name ?? "")
  );
}

function sameRule(a: Partial<Rule>, b: Partial<Rule>): boolean {
  if (a.id && b.id) return a.id === b.id;
  return a.weekday === b.weekday && a.time === b.time && (a.location ?? "") === (b.location ?? "");
}

async function authorised(request: Request, env: Env): Promise<boolean> {
  const header = request.headers.get("Authorization") ?? "";
  const provided = header.startsWith("Bearer ") ? header.slice(7) : "";

  // Hash both to a fixed width before comparing: timingSafeEqual throws on a
  // length mismatch, and bailing out early on length would leak it anyway.
  const encoder = new TextEncoder();
  const [got, want] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(provided)),
    crypto.subtle.digest("SHA-256", encoder.encode(env.DASHBOARD_PASSPHRASE)),
  ]);
  return crypto.subtle.timingSafeEqual(got, want);
}

function corsHeaders(env: Env): Record<string, string> {
  return {
    "Access-Control-Allow-Origin": env.ALLOWED_ORIGIN,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}

function json(body: unknown, env: Env, status = 200): Response {
  return Response.json(body, { status, headers: corsHeaders(env) });
}

async function readBody<T>(request: Request): Promise<T> {
  try {
    return (await request.json()) as T;
  } catch {
    throw new BadRequest("Body must be JSON.");
  }
}

async function armBooking(request: Request, env: Env): Promise<Response> {
  const booking = await readBody<Booking>(request);
  if (!booking?.date || !booking?.time) throw new BadRequest("A booking needs at least a date and a time.");

  let added = false;
  const bookings = await mutate<Booking[]>(
    repoConfig(env),
    PENDING_FILE,
    [],
    `Arm ${booking.name ?? "booking"} at ${booking.time} on ${booking.date}`,
    (current) => {
      const list = Array.isArray(current) ? current : [current as unknown as Booking];
      if (list.some((b) => sameBooking(b, booking))) return list;
      added = true;
      return [...list, booking];
    },
  );

  if (!added) return json({ ok: false, reason: "already_queued", bookings }, env, 409);
  return json({ ok: true, bookings }, env);
}

async function removeBooking(request: Request, env: Env): Promise<Response> {
  const target = await readBody<Partial<Booking>>(request);
  if (!target?.booking_id && !(target?.date && target?.time)) {
    throw new BadRequest("Say which booking: a booking_id, or a date and time.");
  }

  let removed = false;
  const bookings = await mutate<Booking[]>(
    repoConfig(env),
    PENDING_FILE,
    [],
    `Remove pending booking ${target.date ?? ""} ${target.time ?? ""}`.trim(),
    (current) => {
      const list = Array.isArray(current) ? current : [current as unknown as Booking];
      const next = list.filter((b) => !sameBooking(b, target));
      removed = next.length !== list.length;
      return next;
    },
  );

  if (!removed) return json({ ok: false, reason: "not_found", bookings }, env, 404);
  return json({ ok: true, bookings }, env);
}

async function addRule(request: Request, env: Env): Promise<Response> {
  const rule = await readBody<Rule>(request);
  // Mirrors REQUIRED_RULE_FIELDS in bot/standing.py: a rule with no time matches
  // every class at a location, which is a whole day's timetable per run.
  if (!rule?.time) throw new BadRequest("A standing rule needs a time.");

  let added = false;
  const rules = await mutate<Rule[]>(
    repoConfig(env),
    RULES_FILE,
    [],
    `Add standing rule for ${rule.name ?? "class"} on ${rule.weekday ?? "any day"}`,
    (current) => {
      const list = Array.isArray(current) ? current : [];
      if (list.some((r) => sameRule(r, rule))) return list;
      added = true;
      return [...list, { enabled: true, armed_booking_ids: [], ...rule }];
    },
  );

  if (!added) return json({ ok: false, reason: "already_exists", rules }, env, 409);
  return json({ ok: true, rules }, env);
}

async function removeRule(request: Request, env: Env): Promise<Response> {
  const target = await readBody<Partial<Rule>>(request);
  if (!target?.id && !target?.time) throw new BadRequest("Say which rule: an id, or its time.");

  let removed = false;
  const rules = await mutate<Rule[]>(
    repoConfig(env),
    RULES_FILE,
    [],
    `Remove standing rule for ${target.name ?? "class"}`,
    (current) => {
      const list = Array.isArray(current) ? current : [];
      const next = list.filter((r) => !sameRule(r, target));
      removed = next.length !== list.length;
      return next;
    },
  );

  if (!removed) return json({ ok: false, reason: "not_found", rules }, env, 404);
  return json({ ok: true, rules }, env);
}

const ROUTES: Record<string, (request: Request, env: Env) => Promise<Response>> = {
  "/api/bookings": armBooking,
  "/api/bookings/remove": removeBooking,
  "/api/rules": addRule,
  "/api/rules/remove": removeRule,
};

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(env) });
    }

    // A browser on another site can't read the response anyway, but there's no
    // reason to do the work. Absent Origin (curl, tests) is allowed through — the
    // passphrase is the actual gate.
    const origin = request.headers.get("Origin");
    if (origin && origin !== env.ALLOWED_ORIGIN) {
      return json({ error: "Not allowed from this origin." }, env, 403);
    }

    const route = ROUTES[url.pathname];
    if (!route) return json({ error: "Not found." }, env, 404);
    if (request.method !== "POST") return json({ error: "Use POST." }, env, 405);

    try {
      // timingSafeEqual closes the side channel; this closes brute force. The
      // hostname is public and the passphrase is all that stands between the
      // internet and someone else's gym account.
      const ip = request.headers.get("cf-connecting-ip") ?? "unknown";
      const { success } = await env.AUTH_LIMITER.limit({ key: ip });
      if (!success) return json({ error: "Too many attempts. Wait a minute." }, env, 429);

      if (!(await authorised(request, env))) {
        console.warn(JSON.stringify({ message: "rejected", path: url.pathname, ip }));
        return json({ error: "Wrong access key." }, env, 401);
      }

      return await route(request, env);
    } catch (error) {
      if (error instanceof BadRequest) return json({ error: error.message }, env, 400);
      if (error instanceof ConflictError) {
        return json({ error: "The queue was being written to. Try again." }, env, 503);
      }
      console.error(JSON.stringify({
        message: "unhandled error",
        path: url.pathname,
        error: error instanceof Error ? error.message : String(error),
      }));
      return json({ error: "Something went wrong. Try again." }, env, 500);
    }
  },
} satisfies ExportedHandler<Env>;
