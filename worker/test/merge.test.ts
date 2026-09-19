/**
 * A bot run takes ~30s on a 60s tick, so someone arming from the dashboard is
 * very often doing it mid-run. Writing the run's whole list back would drop that
 * booking roughly half the time — silently, which is the failure mode this
 * project keeps being bitten by. These are the cases that has to survive.
 */

import { describe, it, expect } from "vitest";

import { applyPendingDelta, applyRulesDelta, type Booking, type Rule } from "../src/bookings";

const squad: Booking = { booking_id: "b1", date: "2026-09-21", time: "5:15 AM", location: "Marrickville", name: "Squad" };
const pulse: Booking = { booking_id: "b2", date: "2026-09-22", time: "6:00 AM", location: "Haymarket", name: "Pulse" };
const byHand: Booking = { date: "2026-09-23", time: "7:00 AM", location: "Newtown", name: "Athletica" };

describe("what a run changed, applied to what the list is now", () => {
  it("keeps a booking armed from the dashboard while the run was in flight", () => {
    // run pulled [squad], booked it, wants it gone. Meanwhile someone armed pulse.
    const remote = [squad, pulse];
    const next = applyPendingDelta(remote, { remove: [squad] });
    expect(next).toEqual([pulse]);
  });

  it("does not resurrect a booking removed from the dashboard while the run was in flight", () => {
    // run pulled [squad] and only updated its state; the dashboard deleted it
    const next = applyPendingDelta([], { update: [{ match: squad, fields: { state: "full" } }] });
    expect(next).toEqual([]);
  });

  it("lands a strike outcome on the matching entry and leaves the rest alone", () => {
    const next = applyPendingDelta([squad, pulse], {
      update: [{ match: { booking_id: "b1" }, fields: { state: "watching", attempts: 1 } }],
    });
    expect(next[0]).toMatchObject({ booking_id: "b1", state: "watching", attempts: 1 });
    expect(next[1]).toEqual(pulse);
  });

  it("adds what a standing rule armed, without duplicating what is already there", () => {
    const next = applyPendingDelta([squad], { add: [squad, pulse] });
    expect(next).toHaveLength(2);
    expect(next.map((b) => b.booking_id)).toEqual(["b1", "b2"]);
  });

  it("matches a hand-armed booking on date, time and location, having no id", () => {
    expect(applyPendingDelta([byHand], { remove: [{ ...byHand }] })).toEqual([]);
  });

  it("does not confuse two classes sharing a slot at one location", () => {
    const mat = { ...byHand, name: "Mat Pilates" };
    const next = applyPendingDelta([byHand, mat], { remove: [byHand] });
    expect(next).toEqual([mat]);
  });

  it("applies removes before adds, so a re-arm in the same run survives", () => {
    const next = applyPendingDelta([squad], { remove: [squad], add: [squad] });
    expect(next).toEqual([squad]);
  });

  it("leaves the list alone when the run changed nothing", () => {
    const remote = [squad, pulse];
    expect(applyPendingDelta(remote, {})).toEqual(remote);
  });
});

describe("standing rules", () => {
  const weekly: Rule = { id: "r1", weekday: "Monday", time: "5:15 AM", location: "Marrickville", armed_booking_ids: [] };

  it("records what the matcher armed", () => {
    const next = applyRulesDelta([weekly], { armed: [{ match: { id: "r1" }, armed_booking_ids: ["b1"] }] });
    expect(next[0].armed_booking_ids).toEqual(["b1"]);
  });

  it("does not bring back a rule deleted from the dashboard mid-run", () => {
    const next = applyRulesDelta([], { armed: [{ match: { id: "r1" }, armed_booking_ids: ["b1"] }] });
    expect(next).toEqual([]);
  });

  it("leaves a rule the run didn't touch exactly as it was", () => {
    const other: Rule = { id: "r2", weekday: "Friday", time: "6:00 AM", armed_booking_ids: ["x"] };
    const next = applyRulesDelta([weekly, other], { armed: [{ match: { id: "r1" }, armed_booking_ids: ["b1"] }] });
    expect(next[1]).toEqual(other);
  });

  it("never adds a rule — only the dashboard creates those", () => {
    const next = applyRulesDelta([], { armed: [{ match: { id: "nope" }, armed_booking_ids: [] }] });
    expect(next).toEqual([]);
  });
});
