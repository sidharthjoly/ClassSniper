/**
 * The private side of ClassSniper.
 *
 * Two things live here. The GitHub token used to, so the dashboard wouldn't need
 * one — that's what made the thing usable by someone who has never heard of
 * GitHub. And now the personal state: what you booked, what's queued, your
 * standing rules. Those were JSON files in a public repo, readable by anyone,
 * which published a record of where you physically are and when.
 *
 * The public repo keeps the gym's own timetable and a scrape heartbeat. Neither
 * says anything about a person, so the class picker still works with no key at
 * all — only your bookings need one.
 */

import {
  applyPendingDelta,
  applyRulesDelta,
  sameBooking,
  sameRule,
  type Booking,
  type PendingDelta,
  type Rule,
  type RulesDelta,
} from "./bookings";
import { DOC_KEYS, VersionConflict, mutateDoc, readAll, writeDoc, type DocKey } from "./state";

class BadRequest extends Error {}

/** The dashboard's key is handed to whoever uses it; the bot's is not. */
type Role = "dashboard" | "bot";

async function matches(provided: string, expected: string): Promise<boolean> {
  // Hash both to a fixed width first: timingSafeEqual throws on a length
  // mismatch, and bailing out early on length would leak it anyway.
  const encoder = new TextEncoder();
  const [got, want] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(provided)),
    crypto.subtle.digest("SHA-256", encoder.encode(expected)),
  ]);
  return crypto.subtle.timingSafeEqual(got, want);
}

async function identify(request: Request, env: Env): Promise<Role | null> {
  const header = request.headers.get("Authorization") ?? "";
  const provided = header.startsWith("Bearer ") ? header.slice(7) : "";

  // Both are always checked, so the work doesn't reveal which one was close.
  const [isDashboard, isBot] = await Promise.all([
    matches(provided, env.DASHBOARD_PASSPHRASE),
    matches(provided, env.BOT_TOKEN),
  ]);
  if (isBot) return "bot";
  if (isDashboard) return "dashboard";
  return null;
}

function allowedOrigins(env: Env): string[] {
  return env.ALLOWED_ORIGINS.split(",").map((o) => o.trim()).filter(Boolean);
}

function isAllowedOrigin(env: Env, origin: string | null): boolean {
  return origin === null || allowedOrigins(env).includes(origin);
}

function corsHeaders(env: Env, origin: string | null): Record<string, string> {
  const allowed = allowedOrigins(env);
  return {
    // Echo back the one that asked, when it's one of ours. A browser rejects a
    // response naming any origin but its own, so a single hardcoded value breaks
    // the moment the dashboard is served from a second hostname — which is
    // exactly what happened when Pages started serving the custom domain.
    "Access-Control-Allow-Origin": origin && allowed.includes(origin) ? origin : allowed[0],
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}

function json(body: unknown, env: Env, origin: string | null, status = 200): Response {
  return Response.json(body, { status, headers: corsHeaders(env, origin) });
}

async function readBody<T>(request: Request): Promise<T> {
  try {
    return (await request.json()) as T;
  } catch {
    throw new BadRequest("Body must be JSON.");
  }
}

// ---------- what the dashboard and the bot both read ----------

async function getState(_request: Request, env: Env, origin: string | null): Promise<Response> {
  const docs = await readAll(env.STATE_DB);
  return json(
    {
      status: docs.status.value,
      bookings: docs.pending.value,
      rules: docs.rules.value,
      versions: Object.fromEntries(DOC_KEYS.map((k) => [k, docs[k].version])),
    },
    env,
    origin,
  );
}

// ---------- what the dashboard writes ----------

async function armBooking(request: Request, env: Env, origin: string | null): Promise<Response> {
  const booking = await readBody<Booking>(request);
  if (!booking?.date || !booking?.time) throw new BadRequest("A booking needs at least a date and a time.");

  let added = false;
  const bookings = await mutateDoc<Booking[]>(env.STATE_DB, "pending", [], (current) => {
    const list = Array.isArray(current) ? current : [];
    if (list.some((b) => sameBooking(b, booking))) return list;
    added = true;
    return [...list, booking];
  });

  if (!added) return json({ ok: false, reason: "already_queued", bookings }, env, origin, 409);
  return json({ ok: true, bookings }, env, origin);
}

async function removeBooking(request: Request, env: Env, origin: string | null): Promise<Response> {
  const target = await readBody<Partial<Booking>>(request);
  if (!target?.booking_id && !(target?.date && target?.time)) {
    throw new BadRequest("Say which booking: a booking_id, or a date and time.");
  }

  let removed = false;
  const bookings = await mutateDoc<Booking[]>(env.STATE_DB, "pending", [], (current) => {
    const list = Array.isArray(current) ? current : [];
    const next = list.filter((b) => !sameBooking(b, target));
    removed = next.length !== list.length;
    return next;
  });

  if (!removed) return json({ ok: false, reason: "not_found", bookings }, env, origin, 404);
  return json({ ok: true, bookings }, env, origin);
}

async function addRule(request: Request, env: Env, origin: string | null): Promise<Response> {
  const rule = await readBody<Rule>(request);
  // Mirrors REQUIRED_RULE_FIELDS in bot/standing.py: a rule with no time matches
  // every class at a location, which is a whole day's timetable per run.
  if (!rule?.time) throw new BadRequest("A standing rule needs a time.");

  let added = false;
  const rules = await mutateDoc<Rule[]>(env.STATE_DB, "rules", [], (current) => {
    const list = Array.isArray(current) ? current : [];
    if (list.some((r) => sameRule(r, rule))) return list;
    added = true;
    return [...list, { enabled: true, armed_booking_ids: [], ...rule }];
  });

  if (!added) return json({ ok: false, reason: "already_exists", rules }, env, origin, 409);
  return json({ ok: true, rules }, env, origin);
}

async function removeRule(request: Request, env: Env, origin: string | null): Promise<Response> {
  const target = await readBody<Partial<Rule>>(request);
  if (!target?.id && !target?.time) throw new BadRequest("Say which rule: an id, or its time.");

  let removed = false;
  const rules = await mutateDoc<Rule[]>(env.STATE_DB, "rules", [], (current) => {
    const list = Array.isArray(current) ? current : [];
    const next = list.filter((r) => !sameRule(r, target));
    removed = next.length !== list.length;
    return next;
  });

  if (!removed) return json({ ok: false, reason: "not_found", rules }, env, origin, 404);
  return json({ ok: true, rules }, env, origin);
}

// ---------- what the bot writes ----------

interface PushBody {
  pending?: PendingDelta;
  rules?: RulesDelta;
  status?: unknown;
}

/**
 * A finished run, expressed as what it changed rather than what it ended up with.
 * See applyPendingDelta: a run overlaps the moment someone is clicking, so
 * writing back its whole list would drop their booking.
 */
async function pushState(request: Request, env: Env, origin: string | null): Promise<Response> {
  const body = await readBody<PushBody>(request);

  if (body.pending) {
    await mutateDoc<Booking[]>(env.STATE_DB, "pending", [], (current) =>
      applyPendingDelta(Array.isArray(current) ? current : [], body.pending!),
    );
  }
  if (body.rules) {
    await mutateDoc<Rule[]>(env.STATE_DB, "rules", [], (current) =>
      applyRulesDelta(Array.isArray(current) ? current : [], body.rules!),
    );
  }
  if (body.status !== undefined) {
    // Only the striker writes this one, so there's nothing to merge against.
    await writeDoc(env.STATE_DB, "status", body.status);
  }

  const docs = await readAll(env.STATE_DB);
  return json({ ok: true, bookings: docs.pending.value, rules: docs.rules.value }, env, origin);
}

interface Route {
  method: "GET" | "POST";
  roles: Role[];
  handle: (request: Request, env: Env, origin: string | null) => Promise<Response>;
}

const ROUTES: Record<string, Route> = {
  "/api/state": { method: "GET", roles: ["dashboard", "bot"], handle: getState },
  "/api/state/push": { method: "POST", roles: ["bot"], handle: pushState },
  "/api/bookings": { method: "POST", roles: ["dashboard", "bot"], handle: armBooking },
  "/api/bookings/remove": { method: "POST", roles: ["dashboard", "bot"], handle: removeBooking },
  "/api/rules": { method: "POST", roles: ["dashboard", "bot"], handle: addRule },
  "/api/rules/remove": { method: "POST", roles: ["dashboard", "bot"], handle: removeRule },
};

export default {
  async fetch(request: Request, env: Env, _ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    const origin = request.headers.get("Origin");

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(env, origin) });
    }

    // A browser on another site can't read the response anyway, but there's no
    // reason to do the work. Absent Origin (curl, the bot) is allowed through —
    // the key is the actual gate.
    if (!isAllowedOrigin(env, origin)) {
      return json({ error: "Not allowed from this origin." }, env, origin, 403);
    }

    const route = ROUTES[url.pathname];
    if (!route) return json({ error: "Not found." }, env, origin, 404);
    if (request.method !== route.method) return json({ error: `Use ${route.method}.` }, env, origin, 405);

    try {
      // timingSafeEqual closes the side channel; this closes brute force. The
      // hostname is public and the key is all that stands between the internet
      // and someone else's gym account.
      const ip = request.headers.get("cf-connecting-ip") ?? "unknown";
      const { success } = await env.AUTH_LIMITER.limit({ key: ip });
      if (!success) return json({ error: "Too many attempts. Wait a minute." }, env, origin, 429);

      const role = await identify(request, env);
      if (!role || !route.roles.includes(role)) {
        console.warn(JSON.stringify({ message: "rejected", path: url.pathname, role, ip }));
        return json({ error: role ? "Not allowed." : "Wrong access key." }, env, origin, role ? 403 : 401);
      }

      return await route.handle(request, env, origin);
    } catch (error) {
      if (error instanceof BadRequest) return json({ error: error.message }, env, origin, 400);
      if (error instanceof VersionConflict) {
        return json({ error: "The queue was busy being written to. Try again." }, env, origin, 503);
      }
      console.error(JSON.stringify({
        message: "unhandled error",
        path: url.pathname,
        error: error instanceof Error ? error.message : String(error),
      }));
      return json({ error: "Something went wrong. Try again." }, env, origin, 500);
    }
  },
} satisfies ExportedHandler<Env>;
