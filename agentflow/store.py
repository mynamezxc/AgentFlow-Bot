import copy
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

KINDS = ("agents", "tasks", "messages", "memories", "schedules", "posts", "skills", "plans", "runs", "events",
         "projects", "decisions", "tool_events", "artifacts", "skill_versions", "agent_revisions", "channel_outbox", "plugin_accounts", "custom_tools", "mcp_servers",
         "attachments", "checklist", "file_changes", "model_events")


def initial_seed(seed):
    """A real new workspace starts with a general operator, never a preset team."""
    seed = copy.deepcopy(seed)
    atlas = next(a for a in seed["agents"] if a["id"] == "a-lead")
    atlas.update(role="General operator", category="general", skills=[],
                 description="Own goals, organize the team and coordinate verified delivery for the workspace owner.")
    seed["agents"] = [atlas]
    for kind in ("tasks", "posts", "messages", "events", "memories", "schedules", "plans", "runs"):
        seed[kind] = []
    return seed


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def uid(prefix="r"):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class DomainError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Store:
    def __init__(self, directory, seed):
        self.listeners = []  # fn(event, kind, doc) after every create/patch; must be cheap and never raise into the writer
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.depth = 0
        self.conn = sqlite3.connect(self.directory / "workspace.sqlite3", check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("CREATE TABLE IF NOT EXISTS records(kind TEXT,id TEXT,payload TEXT,version INTEGER,PRIMARY KEY(kind,id))")
        self.conn.execute("CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY,value TEXT)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS deliveries(key TEXT PRIMARY KEY,state TEXT,updated TEXT)")
        if not self.conn.execute("SELECT 1 FROM config WHERE key='settings'").fetchone():
            with self.transaction():
                for kind in KINDS:
                    for item in seed.get(kind, []):
                        self._insert(kind, item)
                self.conn.execute("INSERT INTO config VALUES('settings',?)", (json.dumps(seed["settings"]),))
        if os.name != "nt":
            os.chmod(self.directory, 0o700)
            os.chmod(self.directory / "workspace.sqlite3", 0o600)

    def close(self):
        self.conn.close()

    @contextmanager
    def transaction(self):
        with self.lock:
            outer = self.depth == 0
            if outer:
                self.conn.execute("BEGIN IMMEDIATE")
            self.depth += 1
            try:
                yield
                if outer:
                    self.conn.commit()
            except BaseException:
                if outer:
                    self.conn.rollback()
                raise
            finally:
                self.depth -= 1

    def _insert(self, kind, doc):
        doc = copy.deepcopy(doc)
        doc.setdefault("id", uid(kind[0]))
        doc.setdefault("created_at", now())
        doc["updated_at"] = now()
        doc.pop("version", None)
        self.conn.execute("INSERT INTO records VALUES(?,?,?,1)", (kind, doc["id"], json.dumps(doc, ensure_ascii=False)))
        self._notify("create", kind, {**doc, "version": 1})
        return {**doc, "version": 1}

    def _notify(self, event, kind, doc):
        for listener in list(self.listeners):
            try:
                listener(event, kind, doc)
            except Exception:
                pass

    def create(self, kind, doc):
        if kind not in KINDS:
            raise DomainError("Invalid record type.")
        with self.transaction():
            return self._insert(kind, doc)

    def get(self, kind, record_id):
        with self.lock:
            row = self.conn.execute("SELECT payload,version FROM records WHERE kind=? AND id=?", (kind, record_id)).fetchone()
            if not row:
                raise DomainError("Record not found.", 404)
            return {**json.loads(row[0]), "version": row[1]}

    def all(self, kind):
        with self.lock:
            return [{**json.loads(p), "version": v} for p, v in self.conn.execute(
                "SELECT payload,version FROM records WHERE kind=? ORDER BY rowid", (kind,)).fetchall()]

    def position(self, kind, record_id):
        with self.lock:
            row = self.conn.execute("SELECT rowid FROM records WHERE kind=? AND id=?", (kind, record_id)).fetchone()
            return row[0] if row else -1

    def patch(self, kind, record_id, changes, version=None):
        with self.transaction():
            doc = self.get(kind, record_id)
            if version is not None and doc["version"] != version:
                raise DomainError("Data has changed. Reload before saving to avoid overwriting changes.", 409)
            original_version = doc.pop("version")
            doc.update({k: v for k, v in changes.items() if k not in ("id", "version", "created_at")})
            doc["updated_at"] = now()
            self.conn.execute("UPDATE records SET payload=?,version=version+1 WHERE kind=? AND id=?",
                              (json.dumps(doc, ensure_ascii=False), kind, record_id))
            self._notify("patch", kind, {**doc, "version": original_version + 1})
            return {**doc, "version": original_version + 1}

    def settings(self, changes=None):
        with self.lock:
            data = json.loads(self.conn.execute("SELECT value FROM config WHERE key='settings'").fetchone()[0])
            if changes is not None:
                data.update(changes)
                with self.transaction():
                    self.conn.execute("UPDATE config SET value=? WHERE key='settings'", (json.dumps(data),))
            return data

    def event(self, title, detail="", kind="system"):
        return self.create("events", {"title": title, "detail": detail, "type": kind})

    def reserve_delivery(self, key):
        with self.transaction():
            try:
                self.conn.execute("INSERT INTO deliveries VALUES(?,?,?)", (key, "reserved", now()))
                return True
            except sqlite3.IntegrityError:
                return False

    def delivery_state(self, key, state):
        with self.transaction():
            self.conn.execute("UPDATE deliveries SET state=?,updated=? WHERE key=?", (state, now(), key))

    def snapshot(self):
        data={kind:self.all(kind) for kind in KINDS}
        data["attachments"]=[{k:v for k,v in item.items() if k!="text"} for item in data["attachments"]]
        data["file_changes"]=[{k:v for k,v in item.items() if k not in ("before","after")} for item in data["file_changes"]]
        return {**data,"settings":self.settings()}
