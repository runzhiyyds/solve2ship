"""Immutable task revisions with optimistic concurrency and reviewed-content hashes."""

import json
import time
import uuid

from .contracts import ContractError, canonical_json, compile_task


class TaskConflict(RuntimeError):
    pass


class TaskRepository:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def decode(row):
        if row is None:
            raise KeyError("Task not found")
        result = dict(row)
        if "contract_json" in result:
            result["contract"] = json.loads(result.pop("contract_json"))
        return result

    def save(self, definition, reviewed_sha256, *, task_id=None, base_revision=None):
        preview = compile_task(definition)
        if reviewed_sha256 != preview["content_sha256"]:
            raise TaskConflict("review_required")
        if task_id is not None and (type(base_revision) is not int or base_revision < 1):
            raise ContractError("invalid_field", "base_revision")
        now = time.time()
        with self.store.connection(write=True) as db:
            if task_id is None:
                task_id, revision, parent = uuid.uuid4().hex, 1, None
            else:
                latest = db.execute("SELECT revision,content_sha256 FROM task_revisions WHERE task_id=? ORDER BY revision DESC LIMIT 1",
                                    (task_id,)).fetchone()
                if latest is None:
                    raise KeyError("Task not found")
                if latest["revision"] != base_revision:
                    raise TaskConflict("revision_conflict")
                revision, parent = latest["revision"] + 1, latest["content_sha256"]
            db.execute("""INSERT INTO task_revisions(task_id,revision,created_at,parent_sha256,content_sha256,contract_json)
                          VALUES(?,?,?,?,?,?)""",
                       (task_id, revision, now, parent, preview["content_sha256"], canonical_json(preview["contract"])))
        return self.get_revision(task_id, revision)

    def get_revision(self, task_id, revision):
        with self.store.connection() as db:
            return self.decode(db.execute("SELECT * FROM task_revisions WHERE task_id=? AND revision=?",
                                          (task_id, revision)).fetchone())

    def get(self, task_id):
        with self.store.connection() as db:
            db.execute("BEGIN")
            result = self.decode(db.execute("SELECT * FROM task_revisions WHERE task_id=? ORDER BY revision DESC LIMIT 1",
                                            (task_id,)).fetchone())
            result["revisions"] = [dict(row) for row in db.execute(
                "SELECT revision,created_at,content_sha256,parent_sha256 FROM task_revisions WHERE task_id=? ORDER BY revision DESC", (task_id,))]
            return result

    def list(self):
        with self.store.connection() as db:
            rows = db.execute("""SELECT r.task_id,r.revision,r.created_at,r.content_sha256,
                json_extract(r.contract_json,'$.definition.title') AS title,
                json_extract(r.contract_json,'$.dataset.sample_count') AS sample_count,
                json_extract(r.contract_json,'$.evaluation.supervision_mode') AS supervision_mode
                FROM task_revisions r WHERE r.revision=(SELECT MAX(t.revision) FROM task_revisions t WHERE t.task_id=r.task_id)
                ORDER BY r.created_at DESC,r.task_id LIMIT 100""")
            return [dict(row) for row in rows]
