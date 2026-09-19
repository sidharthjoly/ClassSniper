/** Matching and merging for the two lists both the bot and the dashboard write. */

export interface Booking {
  booking_id?: string;
  date: string;
  time: string;
  location?: string;
  name?: string;
  watch_if_full?: boolean;
  [key: string]: unknown;
}

export interface Rule {
  id?: string;
  weekday?: string;
  time: string;
  location?: string;
  name?: string;
  enabled?: boolean;
  watch_if_full?: boolean;
  armed_booking_ids?: string[];
}

/**
 * Same booking, by the fields that don't move.
 *
 * Deliberately ignores state/attempts/last_attempt: the striker writes those
 * between the dashboard loading a list and someone clicking remove, and comparing
 * whole objects would fail to match exactly when a booking has just resolved —
 * the moment people most want it gone.
 */
export function sameBooking(a: Partial<Booking>, b: Partial<Booking>): boolean {
  if (a.booking_id && b.booking_id) return a.booking_id === b.booking_id;
  return (
    a.date === b.date &&
    a.time === b.time &&
    (a.location ?? "") === (b.location ?? "") &&
    (a.name ?? "") === (b.name ?? "")
  );
}

export function sameRule(a: Partial<Rule>, b: Partial<Rule>): boolean {
  if (a.id && b.id) return a.id === b.id;
  return a.weekday === b.weekday && a.time === b.time && (a.location ?? "") === (b.location ?? "");
}

export interface PendingDelta {
  add?: Booking[];
  remove?: Partial<Booking>[];
  update?: { match: Partial<Booking>; fields: Record<string, unknown> }[];
}

export interface RulesDelta {
  armed?: { match: Partial<Rule>; armed_booking_ids: string[] }[];
}

/**
 * Apply what a bot run changed, onto whatever the list looks like now.
 *
 * A run takes ~30s on a 60s tick, so someone arming from the dashboard is very
 * often doing it mid-run. Writing back the run's whole list would drop that
 * booking on the floor about half the time. Expressing the run's work as adds,
 * removes and field updates means it lands on the current list instead of
 * replacing it.
 */
export function applyPendingDelta(current: Booking[], delta: PendingDelta): Booking[] {
  let next = [...current];

  for (const target of delta.remove ?? []) {
    next = next.filter((b) => !sameBooking(b, target));
  }

  for (const { match, fields } of delta.update ?? []) {
    next = next.map((b) => (sameBooking(b, match) ? { ...b, ...fields } : b));
  }

  for (const booking of delta.add ?? []) {
    if (!next.some((b) => sameBooking(b, booking))) next.push(booking);
  }

  return next;
}

/**
 * The bot only ever touches armed_booking_ids, never the rules themselves — so a
 * rule deleted from the dashboard mid-run stays deleted rather than coming back.
 */
export function applyRulesDelta(current: Rule[], delta: RulesDelta): Rule[] {
  return current.map((rule) => {
    const hit = (delta.armed ?? []).find((a) => sameRule(rule, a.match));
    return hit ? { ...rule, armed_booking_ids: hit.armed_booking_ids } : rule;
  });
}
