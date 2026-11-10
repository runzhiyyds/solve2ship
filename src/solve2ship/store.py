"""SQLite queue with transactional claims, leases, cancellation and append-only events."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
import uuid


TERMINAL = {"succeeded", "failed", "cancelled"}
SCHEMA_VERSION = 14


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError('State database must be an owner-controlled regular file')
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        with self.connection() as db:
            # SQLite journal-mode transitions can return BUSY without invoking its
            # connection busy handler. Concurrent first startup must retry this
            # idempotent local operation, before entering the migration transaction.
            deadline = time.monotonic() + 5
            while True:
                try:
                    db.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as error:
                    if error.sqlite_errorcode not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
            # Serialize version detection and migration, including simultaneous startup.
            db.execute("BEGIN IMMEDIATE")
            try:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in range(SCHEMA_VERSION + 1):
                    raise RuntimeError(f"Unsupported database schema version: {version}")
                if version == 0:
                    self.migrate(db, """
                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN
                            ('queued','running','succeeded','failed','cancelled')),
                        created_at REAL NOT NULL, updated_at REAL NOT NULL,
                        attempt INTEGER NOT NULL DEFAULT 0,
                        owner TEXT, lease_until REAL, result TEXT, error TEXT
                    );
                    CREATE TABLE IF NOT EXISTS events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL REFERENCES jobs(id),
                        at REAL NOT NULL, type TEXT NOT NULL, detail TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS events_job ON events(job_id, sequence);
                    CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
                    BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
                    CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
                    BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
                    PRAGMA user_version=1;
                    """)
                    version = 1
                if version == 1:
                    self.migrate(db, """
                        CREATE TABLE task_revisions (
                            task_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
                            created_at REAL NOT NULL, parent_sha256 TEXT,
                            content_sha256 TEXT NOT NULL, contract_json TEXT NOT NULL,
                            PRIMARY KEY(task_id,revision)
                        );
                        CREATE TRIGGER tasks_no_update BEFORE UPDATE ON task_revisions
                        BEGIN SELECT RAISE(ABORT, 'task revisions are immutable'); END;
                        CREATE TRIGGER tasks_no_delete BEFORE DELETE ON task_revisions
                        BEGIN SELECT RAISE(ABORT, 'task revisions are immutable'); END;
                        PRAGMA user_version=2;
                    """)
                    version = 2
                if version == 2:
                    self.migrate(db, """
                        CREATE TABLE budget_accounts(id TEXT PRIMARY KEY, limit_micro INTEGER NOT NULL CHECK(limit_micro>=0));
                        CREATE TABLE model_calls(
                            id TEXT PRIMARY KEY, scope TEXT NOT NULL, purpose TEXT NOT NULL,
                            model TEXT NOT NULL, created_at REAL NOT NULL, reserved_micro INTEGER NOT NULL,
                            state TEXT NOT NULL, charged_micro INTEGER, usage_json TEXT,
                            provider_id TEXT, fingerprint TEXT, error TEXT
                        );
                        CREATE INDEX model_scope ON model_calls(scope);
                        CREATE TABLE searches(
                            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
                            request_id TEXT NOT NULL UNIQUE, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                            state TEXT NOT NULL, owner TEXT, lease_until REAL, error TEXT,
                            FOREIGN KEY(task_id,revision) REFERENCES task_revisions(task_id,revision)
                        );
                        CREATE TABLE candidates(
                            id TEXT PRIMARY KEY, search_id TEXT NOT NULL REFERENCES searches(id),
                            family TEXT NOT NULL, name TEXT NOT NULL, rationale TEXT NOT NULL,
                            artifact_json TEXT NOT NULL, artifact_sha256 TEXT NOT NULL, provenance_json TEXT NOT NULL
                        );
                        CREATE TABLE executions(
                            id INTEGER PRIMARY KEY AUTOINCREMENT, search_id TEXT NOT NULL REFERENCES searches(id),
                            candidate_id TEXT NOT NULL REFERENCES candidates(id), sample_index INTEGER NOT NULL,
                            output_json TEXT, error TEXT, latency_ms REAL NOT NULL, call_id TEXT,
                            label_agreement INTEGER, UNIQUE(candidate_id,sample_index)
                        );
                        CREATE TABLE search_events(
                            sequence INTEGER PRIMARY KEY AUTOINCREMENT, search_id TEXT NOT NULL REFERENCES searches(id),
                            at REAL NOT NULL, type TEXT NOT NULL, detail_json TEXT NOT NULL
                        );
                        CREATE TRIGGER candidates_no_update BEFORE UPDATE ON candidates
                        BEGIN SELECT RAISE(ABORT,'candidate artifacts are immutable'); END;
                        CREATE TRIGGER candidates_no_delete BEFORE DELETE ON candidates
                        BEGIN SELECT RAISE(ABORT,'candidate artifacts are immutable'); END;
                        CREATE TRIGGER executions_no_update BEFORE UPDATE ON executions
                        BEGIN SELECT RAISE(ABORT,'executions are immutable'); END;
                        CREATE TRIGGER executions_no_delete BEFORE DELETE ON executions
                        BEGIN SELECT RAISE(ABORT,'executions are immutable'); END;
                        CREATE TRIGGER search_events_no_update BEFORE UPDATE ON search_events
                        BEGIN SELECT RAISE(ABORT,'search events are append-only'); END;
                        CREATE TRIGGER search_events_no_delete BEFORE DELETE ON search_events
                        BEGIN SELECT RAISE(ABORT,'search events are append-only'); END;
                        PRAGMA user_version=3;
                    """)
                    version = 3
                if version == 3:
                    self.migrate(db, """
                        ALTER TABLE searches ADD COLUMN protocol_version TEXT NOT NULL DEFAULT 'm2';
                        ALTER TABLE searches ADD COLUMN source_search_id TEXT;
                        ALTER TABLE executions ADD COLUMN fidelity TEXT NOT NULL DEFAULT 'smoke';
                        ALTER TABLE executions ADD COLUMN split TEXT NOT NULL DEFAULT 'exploration';
                        ALTER TABLE executions ADD COLUMN judge_json TEXT;
                        CREATE TABLE partitions(search_id TEXT NOT NULL REFERENCES searches(id),sample_index INTEGER NOT NULL,
                            split TEXT NOT NULL,PRIMARY KEY(search_id,sample_index));
                        CREATE TABLE candidate_decisions(id INTEGER PRIMARY KEY AUTOINCREMENT,
                            search_id TEXT NOT NULL REFERENCES searches(id),candidate_id TEXT NOT NULL REFERENCES candidates(id),
                            stage TEXT NOT NULL,action TEXT NOT NULL,reason TEXT NOT NULL,metrics_json TEXT NOT NULL);
                        CREATE TABLE selections(search_id TEXT PRIMARY KEY REFERENCES searches(id),candidate_id TEXT,
                            evidence_json TEXT NOT NULL);
                        CREATE TRIGGER partitions_no_update BEFORE UPDATE ON partitions
                        BEGIN SELECT RAISE(ABORT,'partitions are immutable'); END;
                        CREATE TRIGGER partitions_no_delete BEFORE DELETE ON partitions
                        BEGIN SELECT RAISE(ABORT,'partitions are immutable'); END;
                        CREATE TRIGGER decisions_no_update BEFORE UPDATE ON candidate_decisions
                        BEGIN SELECT RAISE(ABORT,'decisions are immutable'); END;
                        CREATE TRIGGER decisions_no_delete BEFORE DELETE ON candidate_decisions
                        BEGIN SELECT RAISE(ABORT,'decisions are immutable'); END;
                        CREATE TRIGGER selections_no_update BEFORE UPDATE ON selections
                        BEGIN SELECT RAISE(ABORT,'selections are immutable'); END;
                        CREATE TRIGGER selections_no_delete BEFORE DELETE ON selections
                        BEGIN SELECT RAISE(ABORT,'selections are immutable'); END;
                        PRAGMA user_version=4;
                    """)
                    version = 4
                if version == 4:
                    self.migrate(db, """
                        ALTER TABLE searches ADD COLUMN mutation_target TEXT;
                        ALTER TABLE searches ADD COLUMN stop_requested INTEGER NOT NULL DEFAULT 0;
                        CREATE TABLE execution_slots(search_id TEXT NOT NULL,candidate_id TEXT NOT NULL,
                            sample_index INTEGER NOT NULL,root_id TEXT NOT NULL,PRIMARY KEY(candidate_id,sample_index));
                        CREATE INDEX slots_root ON execution_slots(root_id);
                        WITH RECURSIVE tree(id,root_id) AS (
                            SELECT id,id FROM searches WHERE source_search_id IS NULL UNION ALL
                            SELECT s.id,t.root_id FROM searches s JOIN tree t ON s.source_search_id=t.id)
                        INSERT INTO execution_slots SELECT e.search_id,e.candidate_id,e.sample_index,t.root_id
                            FROM executions e JOIN tree t ON t.id=e.search_id;
                        CREATE TABLE mutations(search_id TEXT NOT NULL REFERENCES searches(id),
                            parent_candidate_id TEXT NOT NULL,child_candidate_id TEXT NOT NULL REFERENCES candidates(id),
                            reason TEXT NOT NULL,diff TEXT NOT NULL,before_sha256 TEXT NOT NULL,after_sha256 TEXT NOT NULL);
                        CREATE TRIGGER mutations_no_update BEFORE UPDATE ON mutations
                        BEGIN SELECT RAISE(ABORT,'mutations are immutable'); END;
                        CREATE TRIGGER mutations_no_delete BEFORE DELETE ON mutations
                        BEGIN SELECT RAISE(ABORT,'mutations are immutable'); END;
                        CREATE TRIGGER slots_no_update BEFORE UPDATE ON execution_slots
                        BEGIN SELECT RAISE(ABORT,'execution reservations are immutable'); END;
                        CREATE TRIGGER slots_no_delete BEFORE DELETE ON execution_slots
                        BEGIN SELECT RAISE(ABORT,'execution reservations are immutable'); END;
                        PRAGMA user_version=5;
                    """)
                    version = 5
                if version == 5:
                    self.migrate(db, """
                        ALTER TABLE model_calls ADD COLUMN diagnostics_json TEXT;
                        ALTER TABLE searches ADD COLUMN execution_limit INTEGER NOT NULL DEFAULT 0;
                        UPDATE searches SET execution_limit=COALESCE((SELECT json_extract(t.contract_json,'$.definition.budget.max_candidate_executions')
                            FROM task_revisions t WHERE t.task_id=searches.task_id AND t.revision=searches.revision),0);
                        PRAGMA user_version=6;
                    """)
                    version = 6
                if version == 6:
                    self.migrate(db, """
                        UPDATE searches SET execution_limit=COALESCE((SELECT json_extract(t.contract_json,'$.definition.budget.max_candidate_executions')
                            FROM task_revisions t WHERE t.task_id=searches.task_id AND t.revision=searches.revision),0)
                            WHERE execution_limit=0;
                        PRAGMA user_version=7;
                    """)
                    version = 7
                if version == 7:
                    self.migrate(db, """
                        CREATE TABLE deliveries(
                            id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                            search_id TEXT NOT NULL UNIQUE REFERENCES searches(id),
                            candidate_id TEXT NOT NULL REFERENCES candidates(id),
                            task_id TEXT NOT NULL, revision INTEGER NOT NULL, created_at REAL NOT NULL,
                            frozen_json TEXT NOT NULL);
                        CREATE TABLE final_quarantine(input_sha256 TEXT PRIMARY KEY,
                            delivery_id TEXT NOT NULL REFERENCES deliveries(id));
                        CREATE TABLE delivery_reports(delivery_id TEXT PRIMARY KEY REFERENCES deliveries(id),
                            report_json TEXT NOT NULL);
                        CREATE TABLE delivery_proofs(delivery_id TEXT PRIMARY KEY REFERENCES deliveries(id),
                            proof_json TEXT NOT NULL);
                        CREATE TRIGGER deliveries_no_update BEFORE UPDATE ON deliveries
                        BEGIN SELECT RAISE(ABORT,'frozen deliveries are immutable'); END;
                        CREATE TRIGGER deliveries_no_delete BEFORE DELETE ON deliveries
                        BEGIN SELECT RAISE(ABORT,'frozen deliveries are immutable'); END;
                        CREATE TRIGGER quarantine_no_update BEFORE UPDATE ON final_quarantine
                        BEGIN SELECT RAISE(ABORT,'exposed holdout cannot be resealed'); END;
                        CREATE TRIGGER quarantine_no_delete BEFORE DELETE ON final_quarantine
                        BEGIN SELECT RAISE(ABORT,'exposed holdout cannot be resealed'); END;
                        CREATE TRIGGER reports_no_update BEFORE UPDATE ON delivery_reports
                        BEGIN SELECT RAISE(ABORT,'final reports are immutable'); END;
                        CREATE TRIGGER reports_no_delete BEFORE DELETE ON delivery_reports
                        BEGIN SELECT RAISE(ABORT,'final reports are immutable'); END;
                        CREATE TRIGGER proofs_no_update BEFORE UPDATE ON delivery_proofs
                        BEGIN SELECT RAISE(ABORT,'delivery proofs are immutable'); END;
                        CREATE TRIGGER proofs_no_delete BEFORE DELETE ON delivery_proofs
                        BEGIN SELECT RAISE(ABORT,'delivery proofs are immutable'); END;
                        PRAGMA user_version=8;
                    """)
                    version = 8
                if version == 8:
                    self.migrate(db, """
                        ALTER TABLE model_calls ADD COLUMN pricing_json TEXT;
                        UPDATE model_calls SET pricing_json='{"model":"deepseek-flash","verified_date":"2026-11-07","currency":"CNY","input_micro_per_token":2,"output_micro_per_token":8,"context_ceiling":1048576,"source":"https://api-docs.deepseek.com/zh-cn/quick_start/pricing/","legacy_snapshot_backfill":true}';
                        CREATE TRIGGER pricing_no_update BEFORE UPDATE OF pricing_json ON model_calls
                        BEGIN SELECT RAISE(ABORT,'call pricing is immutable'); END;
                        PRAGMA user_version=9;
                    """)
                    version = 9
                if version == 9:
                    self.migrate(db, """
                        ALTER TABLE executions ADD COLUMN runtime_json TEXT;
                        CREATE TABLE service_exports(id TEXT PRIMARY KEY, delivery_id TEXT NOT NULL UNIQUE REFERENCES deliveries(id),
                            created_at REAL NOT NULL, receipt_json TEXT NOT NULL);
                        CREATE TRIGGER exports_no_update BEFORE UPDATE ON service_exports
                        BEGIN SELECT RAISE(ABORT,'export receipts are immutable'); END;
                        CREATE TRIGGER exports_no_delete BEFORE DELETE ON service_exports
                        BEGIN SELECT RAISE(ABORT,'export receipts are immutable'); END;
                        PRAGMA user_version=10;
                    """)
                    version = 10
                if version == 10:
                    self.migrate(db, """
                        CREATE TABLE service_export_versions(id TEXT PRIMARY KEY, delivery_id TEXT NOT NULL REFERENCES deliveries(id),
                            created_at REAL NOT NULL, receipt_json TEXT NOT NULL);
                        INSERT INTO service_export_versions SELECT * FROM service_exports;
                        CREATE INDEX export_versions_delivery ON service_export_versions(delivery_id,created_at);
                        CREATE TRIGGER export_versions_no_update BEFORE UPDATE ON service_export_versions
                        BEGIN SELECT RAISE(ABORT,'export versions are immutable'); END;
                        CREATE TRIGGER export_versions_no_delete BEFORE DELETE ON service_export_versions
                        BEGIN SELECT RAISE(ABORT,'export versions are immutable'); END;
                        PRAGMA user_version=11;
                    """)
                    version = 11
                if version == 11:
                    self.migrate(db,"""
                        CREATE TABLE service_budget_grants(id TEXT PRIMARY KEY,delivery_id TEXT NOT NULL UNIQUE REFERENCES deliveries(id),
                            root_id TEXT NOT NULL,created_at REAL NOT NULL,amount_micro INTEGER NOT NULL CHECK(amount_micro>0));
                        CREATE TRIGGER grants_no_update BEFORE UPDATE ON service_budget_grants
                        BEGIN SELECT RAISE(ABORT,'service allowances are immutable'); END;
                        CREATE TRIGGER grants_no_delete BEFORE DELETE ON service_budget_grants
                        BEGIN SELECT RAISE(ABORT,'service allowances are immutable'); END;
                        PRAGMA user_version=12;
                    """)
                    version = 12
                if version == 12:
                    self.migrate(db,"""
                        CREATE TABLE criteria_attempts(root_id TEXT PRIMARY KEY REFERENCES searches(id),created_at REAL NOT NULL);
                        CREATE TABLE criteria_bundles(root_id TEXT PRIMARY KEY REFERENCES searches(id),created_at REAL NOT NULL,bundle_json TEXT NOT NULL);
                        ALTER TABLE executions ADD COLUMN criteria_json TEXT;
                        CREATE TRIGGER criteria_attempts_no_update BEFORE UPDATE ON criteria_attempts
                        BEGIN SELECT RAISE(ABORT,'criteria attempts are immutable'); END;
                        CREATE TRIGGER criteria_attempts_no_delete BEFORE DELETE ON criteria_attempts
                        BEGIN SELECT RAISE(ABORT,'criteria attempts are immutable'); END;
                        CREATE TRIGGER criteria_bundles_no_update BEFORE UPDATE ON criteria_bundles
                        BEGIN SELECT RAISE(ABORT,'criteria bundles are immutable'); END;
                        CREATE TRIGGER criteria_bundles_no_delete BEFORE DELETE ON criteria_bundles
                        BEGIN SELECT RAISE(ABORT,'criteria bundles are immutable'); END;
                        PRAGMA user_version=13;
                    """)
                    version = 13
                if version == 13:
                    self.migrate(db,"""
                        CREATE TABLE quality_review_events(id TEXT PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,request_sha256 TEXT NOT NULL,
                            created_at REAL NOT NULL,search_id TEXT NOT NULL REFERENCES searches(id),subject_kind TEXT NOT NULL,
                            execution_id INTEGER REFERENCES executions(id),delivery_id TEXT REFERENCES deliveries(id),sample_index INTEGER NOT NULL,
                            snapshot_sha256 TEXT NOT NULL,previous_review_id TEXT REFERENCES quality_review_events(id),review_json TEXT NOT NULL);
                        CREATE TABLE delivery_quality_decisions(delivery_id TEXT PRIMARY KEY REFERENCES deliveries(id),request_id TEXT NOT NULL UNIQUE,
                            request_sha256 TEXT NOT NULL,created_at REAL NOT NULL,decision_json TEXT NOT NULL);
                        CREATE TRIGGER reviews_no_update BEFORE UPDATE ON quality_review_events
                        BEGIN SELECT RAISE(ABORT,'quality reviews are immutable'); END;
                        CREATE TRIGGER reviews_no_delete BEFORE DELETE ON quality_review_events
                        BEGIN SELECT RAISE(ABORT,'quality reviews are immutable'); END;
                        CREATE TRIGGER quality_decisions_no_update BEFORE UPDATE ON delivery_quality_decisions
                        BEGIN SELECT RAISE(ABORT,'quality decisions are immutable'); END;
                        CREATE TRIGGER quality_decisions_no_delete BEFORE DELETE ON delivery_quality_decisions
                        BEGIN SELECT RAISE(ABORT,'quality decisions are immutable'); END;
                        PRAGMA user_version=14;
                    """)
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def migrate(db, script):
        # executescript commits an existing transaction; execute complete statements
        # individually so migration errors roll back schema + version together.
        statement = ""
        for line in script.splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                db.execute(statement)
                statement = ""
        if statement.strip():
            raise RuntimeError("Incomplete migration statement")

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def event(db, job_id, kind, detail, now):
        db.execute("INSERT INTO events(job_id, at, type, detail) VALUES(?,?,?,?)",
                   (job_id, now, kind, json.dumps(detail)))

    @staticmethod
    def decode(row):
        if row is None:
            raise KeyError("Job not found")
        job = dict(row)
        job.pop("owner")
        job.pop("lease_until")
        job["result"] = json.loads(job["result"]) if job["result"] else None
        return job

    def create(self, kind="foundation_check"):
        if kind != "foundation_check":
            raise ValueError("Only foundation_check is implemented in M0")
        now, job_id = time.time(), uuid.uuid4().hex
        with self.connection(write=True) as db:
            db.execute("INSERT INTO jobs(id,kind,state,created_at,updated_at) VALUES(?,?,'queued',?,?)",
                       (job_id, kind, now, now))
            self.event(db, job_id, "queued", {"kind": kind}, now)
        return self.get(job_id)

    def get(self, job_id):
        with self.connection() as db:
            # A single read transaction keeps the job snapshot and event log consistent.
            db.execute("BEGIN")
            job = self.decode(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            job["events"] = [dict(e) for e in db.execute(
                "SELECT sequence,at,type,detail FROM events WHERE job_id=? ORDER BY sequence", (job_id,))]
            for event in job["events"]:
                event["detail"] = json.loads(event["detail"])
            return job

    def list(self):
        with self.connection() as db:
            return [self.decode(row) for row in db.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT 100")]

    def cancel(self, job_id):
        with self.connection(write=True) as db:
            job = self.decode(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if job["state"] not in TERMINAL:
                now = time.time()
                db.execute("UPDATE jobs SET state='cancelled',updated_at=?,owner=NULL,lease_until=NULL WHERE id=?",
                           (now, job_id))
                self.event(db, job_id, "cancelled", {}, now)
        return self.get(job_id)

    def claim(self, *, now=None, lease_seconds=30):
        now = time.time() if now is None else now
        if lease_seconds <= 0:
            raise ValueError("Lease must be positive")
        with self.connection(write=True) as db:
            expired = db.execute("SELECT * FROM jobs WHERE state='running' AND lease_until<=?", (now,)).fetchall()
            for row in expired:
                exhausted = row["attempt"] >= 3
                db.execute("UPDATE jobs SET state=?,owner=NULL,lease_until=NULL,updated_at=?,error=? WHERE id=?",
                           ("failed" if exhausted else "queued", now,
                            "Recovery attempts exhausted" if exhausted else None, row["id"]))
                self.event(db, row["id"], "recovery_exhausted" if exhausted else "requeued",
                           {"reason": "worker_lease_expired"}, now)
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None:
                return None
            owner = uuid.uuid4().hex
            db.execute("UPDATE jobs SET state='running',owner=?,lease_until=?,updated_at=?,attempt=attempt+1 WHERE id=?",
                       (owner, now + lease_seconds, now, row["id"]))
            self.event(db, row["id"], "started", {"attempt": row["attempt"] + 1}, now)
            return row["id"], owner

    def finish(self, job_id, owner, *, result=None, error=None, now=None):
        now = time.time() if now is None else now
        with self.connection(write=True) as db:
            state = "failed" if error else "succeeded"
            changed = db.execute("""UPDATE jobs SET state=?,result=?,error=?,updated_at=?,owner=NULL,lease_until=NULL
                WHERE id=? AND owner=? AND state='running' AND lease_until>?""",
                (state, json.dumps(result) if result is not None else None, error, now, job_id, owner, now)).rowcount
            if changed:
                self.event(db, job_id, state, {"evidence_kind": "infrastructure_diagnostic"}, now)
            return bool(changed)
