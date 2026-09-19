/**
 * Scoped deliberately to the gate. This endpoint can arm bookings on a real gym
 * account and its hostname is public, so the passphrase check is the one piece
 * where a silent bug means someone else books your classes. The GitHub mutation
 * path is verified by hand against a real repo — a stubbed GitHub would only ever
 * confirm the stub.
 */

import { env, createExecutionContext, waitOnExecutionContext } from "cloudflare:test";
import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";

import worker from "../src/index";

const PASS = "correct-horse-battery-staple";
const ORIGIN = "https://classsniper.sidharthjoly.com";
const FALLBACK_ORIGIN = "https://sidharthjoly.github.io";

async function call(path: string, init: RequestInit = {}) {
  const request = new Request(`https://api.test${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
    body: init.body ?? JSON.stringify({ date: "2026-09-21", time: "6:00 AM" }),
    ...(init.method ? { method: init.method } : {}),
  });
  const ctx = createExecutionContext();
  const response = await worker.fetch(request, env, ctx);
  await waitOnExecutionContext(ctx);
  return response;
}

const withKey = (key: string) => ({ Authorization: `Bearer ${key}` });

beforeEach(() => {
  // Stop any authorised request from actually reaching GitHub.
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify({ content: btoa("[]"), sha: "abc" }), { status: 200 }),
  );
});

afterEach(() => vi.restoreAllMocks());

describe("the gate", () => {
  it("turns away a request with no key at all", async () => {
    expect((await call("/api/bookings")).status).toBe(401);
  });

  it("turns away the wrong key", async () => {
    expect((await call("/api/bookings", { headers: withKey("hunter2") })).status).toBe(401);
  });

  it("turns away a key that is a prefix of the right one", async () => {
    expect((await call("/api/bookings", { headers: withKey(PASS.slice(0, -1)) })).status).toBe(401);
  });

  it("turns away the right key sent the wrong way", async () => {
    expect((await call("/api/bookings", { headers: { Authorization: PASS } })).status).toBe(401);
  });

  it("does not leak the key in a rejection", async () => {
    const body = await (await call("/api/bookings", { headers: withKey("nope") })).text();
    expect(body).not.toContain(PASS);
  });

  it("lets the right key through to the handler", async () => {
    const res = await call("/api/bookings", { headers: withKey(PASS) });
    expect(res.status).not.toBe(401);
    expect(res.status).not.toBe(403);
  });

  it("guards every route that writes", async () => {
    for (const path of ["/api/bookings", "/api/bookings/remove", "/api/rules", "/api/rules/remove"]) {
      expect((await call(path)).status, `${path} was unguarded`).toBe(401);
    }
  });
});

describe("origin handling", () => {
  it("answers the preflight the dashboard sends", async () => {
    const res = await call("/api/bookings", { method: "OPTIONS", headers: { Origin: ORIGIN } });
    expect(res.status).toBe(204);
    expect(res.headers.get("Access-Control-Allow-Origin")).toBe(ORIGIN);
    expect(res.headers.get("Access-Control-Allow-Headers")).toContain("Authorization");
  });

  it("refuses another site, key or no key", async () => {
    const res = await call("/api/bookings", {
      headers: { ...withKey(PASS), Origin: "https://not-the-dashboard.example" },
    });
    expect(res.status).toBe(403);
  });

  /**
   * Pages serves the dashboard from a custom domain and 301s the github.io URL to
   * it. A single hardcoded origin passed every test and then broke arming on the
   * live site, because a browser rejects a response naming any origin but its own.
   */
  it.each([ORIGIN, FALLBACK_ORIGIN])("echoes back %s, so the browser accepts it", async (origin) => {
    for (const method of ["OPTIONS", "POST"] as const) {
      const res = await call("/api/bookings", {
        method,
        headers: { ...withKey(PASS), Origin: origin },
      });
      expect(res.headers.get("Access-Control-Allow-Origin"), `${method} from ${origin}`).toBe(origin);
    }
  });
});

describe("shape", () => {
  it("rejects a booking with no date or time", async () => {
    const res = await call("/api/bookings", { headers: withKey(PASS), body: JSON.stringify({ name: "Athletica" }) });
    expect(res.status).toBe(400);
  });

  it("rejects a standing rule with no time, like the matcher does", async () => {
    const res = await call("/api/rules", {
      headers: withKey(PASS),
      body: JSON.stringify({ weekday: "Monday", location: "Newtown" }),
    });
    expect(res.status).toBe(400);
  });

  it("rejects a body that isn't JSON", async () => {
    const res = await call("/api/bookings", { headers: withKey(PASS), body: "not json" });
    expect(res.status).toBe(400);
  });

  it("404s an unknown path without checking the key", async () => {
    expect((await call("/api/whatever", { headers: withKey(PASS) })).status).toBe(404);
  });

  it("405s a GET to a real route", async () => {
    const request = new Request("https://api.test/api/bookings", { method: "GET", headers: withKey(PASS) });
    const ctx = createExecutionContext();
    const res = await worker.fetch(request, env, ctx);
    await waitOnExecutionContext(ctx);
    expect(res.status).toBe(405);
  });
});
