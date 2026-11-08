-- Frozen local M0 schema, used only to exercise a real v1 -> v2 migration.
CREATE TABLE jobs (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','cancelled')),
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    owner TEXT, lease_until REAL, result TEXT, error TEXT
);
CREATE TABLE events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    at REAL NOT NULL, type TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE INDEX events_job ON events(job_id,sequence);
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
PRAGMA user_version=1;
