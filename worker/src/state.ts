/**
 * The private half of ClassSniper: what you booked, what's queued, your standing
 * rules.
 *
 * These were JSON files in a public repo, read by the dashboard with no
 * credential. That published which class you'd booked, at which gym, at what
 * time — a record of where you physically are — to anyone who opened the page or
 * the repo. The public repo now carries only the gym's own timetable and a
 * scrape heartbeat, neither of which says anything about a person.
 */

export type DocKey = "pending" | "rules" | "status";
export const DOC_KEYS: DocKey[] = ["pending", "rules", "status"];

export interface Doc<T = unknown> {
  value: T;
  version: number;
}

export class VersionConflict extends Error {}

function parse<T>(raw: string, fallback: T): T {
  try {
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

export async function readDoc<T>(db: D1Database, key: DocKey, fallback: T): Promise<Doc<T>> {
  const row = await db
    .prepare("SELECT value, version FROM state WHERE key = ?")
    .bind(key)
    .first<{ value: string; version: number }>();

  if (!row) return { value: fallback, version: 0 };
  return { value: parse<T>(row.value, fallback), version: row.version };
}

export async function readAll(db: D1Database): Promise<Record<DocKey, Doc>> {
  const { results } = await db
    .prepare("SELECT key, value, version FROM state")
    .all<{ key: DocKey; value: string; version: number }>();

  const out = {} as Record<DocKey, Doc>;
  for (const key of DOC_KEYS) {
    const row = results.find((r) => r.key === key);
    const fallback = key === "status" ? {} : [];
    out[key] = row ? { value: parse(row.value, fallback), version: row.version } : { value: fallback, version: 0 };
  }
  return out;
}

/**
 * Read, change, write, with the version checked on the way in.
 *
 * Both the dashboard and the bot write `pending`, and the bot's run overlaps the
 * window in which someone is clicking. The version check turns a lost update into
 * a retry instead of a booking that silently vanishes.
 */
export async function mutateDoc<T>(
  db: D1Database,
  key: DocKey,
  fallback: T,
  change: (current: T) => T,
): Promise<T> {
  for (let attempt = 0; attempt < 4; attempt++) {
    const { value, version } = await readDoc<T>(db, key, fallback);
    const next = change(value);
    if (JSON.stringify(next) === JSON.stringify(value)) return next;

    const written = await db
      .prepare(
        version === 0
          ? "INSERT INTO state (key, value, version, updated_at) VALUES (?1, ?2, 1, datetime('now')) ON CONFLICT(key) DO NOTHING"
          : "UPDATE state SET value = ?2, version = version + 1, updated_at = datetime('now') WHERE key = ?1 AND version = ?3",
      )
      .bind(...(version === 0 ? [key, JSON.stringify(next)] : [key, JSON.stringify(next), version]))
      .run();

    if (written.meta.changes > 0) return next;
  }
  throw new VersionConflict(`${key} kept changing underneath`);
}

export async function writeDoc(db: D1Database, key: DocKey, value: unknown): Promise<void> {
  await db
    .prepare(
      "INSERT INTO state (key, value, version, updated_at) VALUES (?1, ?2, 1, datetime('now')) " +
        "ON CONFLICT(key) DO UPDATE SET value = ?2, version = version + 1, updated_at = datetime('now')",
    )
    .bind(key, JSON.stringify(value))
    .run();
}
