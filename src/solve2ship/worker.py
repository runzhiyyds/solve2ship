"""Only trusted, repository-authored infrastructure diagnostics run here."""

import platform
import sqlite3
import threading

from .contracts import DisabledSandbox
from .store import SCHEMA_VERSION


def foundation_check(store):
    with store.connection() as db:
        integrity = db.execute("PRAGMA quick_check").fetchone()[0]
        schema = db.execute("PRAGMA user_version").fetchone()[0]
    if integrity != "ok" or schema != SCHEMA_VERSION:
        raise RuntimeError("Database health check failed")
    return {
        "evidence_kind": "infrastructure_diagnostic",
        "checks": {"sqlite_integrity": integrity, "schema_version": schema,
                   "python_version": platform.python_version(),
                   "sqlite_version": sqlite3.sqlite_version,
                   "candidate_execution_enabled": True, 'arbitrary_code_execution_enabled': DisabledSandbox.available},
        "scope": "M0 infrastructure only; no candidates, evaluation or optimization executed",
    }


class Worker:
    def __init__(self, store):
        self.store = store
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="sts-worker", daemon=True)

    def tick(self):
        claim = self.store.claim()
        if claim is None:
            return False
        job_id, owner = claim
        try:
            # Dispatch is a trusted allowlist. Never import/exec candidate artifacts.
            if self.store.get(job_id)["kind"] != "foundation_check":
                raise ValueError("Unsupported job kind")
            result = foundation_check(self.store)
        except Exception:
            # Do not persist arbitrary exception text, which can contain secrets.
            self.store.finish(job_id, owner, error="Infrastructure check failed")
        else:
            self.store.finish(job_id, owner, result=result)
        return True

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except sqlite3.Error:
                # A transient lock does not silently kill the worker; leases permit retry.
                pass
            self.stop.wait(0.2)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=6)
