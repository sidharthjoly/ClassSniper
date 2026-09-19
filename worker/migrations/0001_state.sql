-- Personal state used to live as JSON files in a public repo, which published
-- which class you booked, where and when, to anyone who looked. It lives here
-- instead. The shape is deliberately still "a JSON document per key": the bot
-- scripts work on whole documents, and modelling this relationally would buy
-- nothing but a migration.
CREATE TABLE IF NOT EXISTS state (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  version    INTEGER NOT NULL DEFAULT 1,  -- bumped on every write, for optimistic concurrency
  updated_at TEXT NOT NULL
);

INSERT OR IGNORE INTO state (key, value, version, updated_at) VALUES
  ('pending', '[]', 1, datetime('now')),
  ('rules',   '[]', 1, datetime('now')),
  ('status',  '{}', 1, datetime('now'));
