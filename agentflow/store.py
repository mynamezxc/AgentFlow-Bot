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
        # Change feed for GET /api/state?since=<rev>: every create/patch/delete bumps `seq` and stamps the record. The epoch makes a
        # cursor from an earlier process unusable (the client then reloads everything).
        self.epoch = uuid.uuid4().hex[:8]
        self.seq = 1
        self.changed = {}          # (kind, id) -> seq of the latest write
        self.tombstones = {}       # (kind, id) -> seq of the delete
        self.settings_seq = 0
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
        self.mark_changed(kind, doc["id"])
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
            rows = self.conn.execute("SELECT payload,version FROM records WHERE kind=? ORDER BY rowid", (kind,)).fetchall()
        return [{**json.loads(p), "version": v} for p, v in rows]   # parsed outside the lock: large kinds must not stall writers

    def find(self, kind, field, values):
        """Records of a kind whose top-level `field` is one of `values`, in creation order (without parsing the others)."""
        values = list(values)
        with self.lock:
            try:
                rows = self.conn.execute("SELECT payload,version FROM records WHERE kind=? AND json_extract(payload,?) IN (%s) ORDER BY rowid" % ",".join("?" * len(values)),
                                         (kind, "$." + field, *values)).fetchall()
            except sqlite3.OperationalError:   # SQLite without JSON1
                rows = [r for r in self.conn.execute("SELECT payload,version FROM records WHERE kind=? ORDER BY rowid", (kind,)).fetchall()
                        if json.loads(r[0]).get(field) in values]
        return [{**json.loads(p), "version": v} for p, v in rows]

    def count_by(self, kind, field):
        """{value: number of records} of a top-level field, computed by SQLite."""
        with self.lock:
            try:
                return {v: n for v, n in self.conn.execute("SELECT json_extract(payload,?),count(*) FROM records WHERE kind=? GROUP BY 1", ("$." + field, kind))}
            except sqlite3.OperationalError:
                counts = {}
                for (p,) in self.conn.execute("SELECT payload FROM records WHERE kind=?", (kind,)):
                    value = json.loads(p).get(field)
                    counts[value] = counts.get(value, 0) + 1
                return counts

    # ---- change feed -------------------------------------------------------------------------------------------------
    def mark_changed(self, kind, record_id):
        """Stamp a record as written now (also called by code that updates the table directly)."""
        with self.lock:
            self.seq += 1
            self.changed[(kind, record_id)] = self.seq
            self.tombstones.pop((kind, record_id), None)

    def delete(self, kind, record_id):
        if kind not in KINDS:
            raise DomainError("Invalid record type.")
        with self.transaction():
            self.get(kind, record_id)
            self.conn.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, record_id))
            self.seq += 1
            self.changed.pop((kind, record_id), None)
            self.tombstones[(kind, record_id)] = self.seq

    def cursor(self):
        return "%s.%d" % (self.epoch, self.seq)

    def parse_cursor(self, value):
        """The sequence number a client cursor stands for, or None when it is missing, malformed or from another process."""
        try:
            epoch, number = str(value or "").split(".", 1)
            number = int(number)
        except ValueError:
            return None
        return number if epoch == self.epoch and 0 <= number <= self.seq else None

    def rows_since(self, seq):
        """(changed rows [(rowid, kind, id, payload, version)], deleted [(kind, id)], settings_changed) after `seq`, consistent under the lock."""
        with self.lock:
            keys = [key for key, number in self.changed.items() if number > seq]
            rows = []
            for kind in {k for k, _ in keys}:
                ids = [i for k, i in keys if k == kind]
                for start in range(0, len(ids), 400):
                    chunk = ids[start:start + 400]
                    rows += [(r, kind, i, p, v) for r, i, p, v in self.conn.execute(
                        "SELECT rowid,id,payload,version FROM records WHERE kind=? AND id IN (%s)" % ",".join("?" * len(chunk)), (kind, *chunk))]
            rows.sort()
            return rows, [key for key, number in self.tombstones.items() if number > seq], self.settings_seq > seq

    def latest_rows(self, kind, limit):
        """The newest `limit` rows of a kind, oldest first: [(rowid, id, payload, version)]."""
        with self.lock:
            rows = self.conn.execute("SELECT rowid,id,payload,version FROM records WHERE kind=? ORDER BY rowid DESC LIMIT ?", (kind, int(limit))).fetchall()
        return rows[::-1]

    def message_window(self, per_channel, decision_ids=()):
        """Message rows for the state: the newest `per_channel` of every channel plus the messages of `decision_ids`, in creation order.
        Also the total, the oldest window message of each channel and the channels that have older messages."""
        decision_ids = set(decision_ids)
        with self.lock:
            try:
                index = self.conn.execute("SELECT rowid,id,json_extract(payload,'$.channel'),json_extract(payload,'$.decision_id') FROM records WHERE kind='messages' ORDER BY rowid").fetchall()
            except sqlite3.OperationalError:   # SQLite without JSON1
                index = []
                for r, i, p in self.conn.execute("SELECT rowid,id,payload FROM records WHERE kind='messages' ORDER BY rowid"):
                    doc = json.loads(p)
                    index.append((r, i, doc.get("channel"), doc.get("decision_id")))
            counts, keep, first, older = {}, set(), {}, set()
            for rowid, record_id, channel, decision in reversed(index):
                counts[channel] = counts.get(channel, 0) + 1
                if counts[channel] <= per_channel:
                    keep.add(record_id)
                    first[channel] = record_id      # ends as the oldest message of the window
                else:
                    older.add(channel)
                if decision and decision in decision_ids:
                    keep.add(record_id)
            wanted = [r[1] for r in index if r[1] in keep]
            rows = []
            for start in range(0, len(wanted), 400):
                chunk = wanted[start:start + 400]
                rows += self.conn.execute("SELECT rowid,id,payload,version FROM records WHERE kind='messages' AND id IN (%s)" % ",".join("?" * len(chunk)), chunk).fetchall()
            rows.sort()
            return rows, len(index), first, sorted(older, key=str)

    def messages_page(self, channel, before=None, limit=100):
        """Older messages of one channel: up to `limit` messages created before the message `before`, oldest first, and whether more exist."""
        limit = max(1, min(int(limit), 500))
        with self.lock:
            marker = self.position("messages", before) if before else None
            if before and marker < 0:
                raise DomainError("Message not found.", 404)
            args = [channel] + ([marker] if marker else [])
            rows = self.conn.execute("SELECT rowid,id,payload,version FROM records WHERE kind='messages' AND json_extract(payload,'$.channel')=?"
                                     + (" AND rowid<?" if marker else "") + " ORDER BY rowid DESC LIMIT ?", (*args, limit + 1)).fetchall()
        more = len(rows) > limit
        return [{**json.loads(p), "version": v} for _, _, p, v in rows[:limit][::-1]], more

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
            self.mark_changed(kind, record_id)
            self._notify("patch", kind, {**doc, "version": original_version + 1})
            return {**doc, "version": original_version + 1}

    def settings(self, changes=None):
        with self.lock:
            data = json.loads(self.conn.execute("SELECT value FROM config WHERE key='settings'").fetchone()[0])
            if changes is not None:
                data.update(changes)
                with self.transaction():
                    self.conn.execute("UPDATE config SET value=? WHERE key='settings'", (json.dumps(data),))
                    self.seq += 1
                    self.settings_seq = self.seq
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
