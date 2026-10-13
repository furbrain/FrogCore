-- Already applied to the production D1 database "frogcore". Kept for reference / local dev:
--   npx wrangler d1 execute frogcore --local --file=schema.sql
CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  pw_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE sessions (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  expires_at INTEGER NOT NULL
);
CREATE TABLE surveys (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'uploading',  -- uploading | building | ready | failed
  error TEXT,
  building_since INTEGER,
  created_at INTEGER NOT NULL
);
CREATE INDEX surveys_user ON surveys(user_id);
