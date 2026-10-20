"""The browser's view of the workspace: a slim /api/state, incremental deltas and cached, gzip-ready payloads.

The page only needs what it renders. Run traces, native sessions, model history, stored tool results and the other bookkeeping
records stay in the database (and in /api/export, which is never slimmed); /api/state carries the rest, with the newest messages
of every channel, and `since=<rev>` returns only the records written after the client's last response.
"""
import gzip
import hashlib
import json
import threading

from .store import KINDS

# Run fields the page never reads; the engine keeps them in the database.
RUN_HIDDEN = frozenset(("trace", "native_session", "pending_calls", "model_history", "operation_fingerprints", "last_output_hash",
                        "account_fingerprint"))
# Record types the page never reads from the state (it fetches /api/mcp/servers etc. on demand).
OMITTED = frozenset(("channel_outbox", "model_events", "mcp_servers", "skill_versions", "agent_revisions", "tool_events"))
MESSAGES_PER_CHANNEL = 300
TOOL_EVENTS_LATEST = 30      # newest tool events in a full state; the rest is /api/runs/<id>/events
TOOL_TEXT = 300              # characters of arguments/result kept on those


def _clip(value, limit):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "..."


def project(kind, doc):
    """The slim form of a record for the page."""
    if kind == "runs":
        return {k: v for k, v in doc.items() if k not in RUN_HIDDEN}
    if kind == "tool_events":
        return {**doc, "arguments": _clip(doc.get("arguments", ""), TOOL_TEXT), "result": _clip(doc.get("result", ""), TOOL_TEXT), "clipped": True}
    if kind == "attachments":
        return {k: v for k, v in doc.items() if k != "text"}
    if kind == "file_changes":
        return {k: v for k, v in doc.items() if k not in ("before", "after")}
    return doc


PROJECTED = frozenset(("runs", "tool_events", "attachments", "file_changes"))


def gz(raw):
    return gzip.compress(raw, 5, mtime=0)


class StateFeed:
    def __init__(self, store, runtime):
        """`runtime()` returns the small, always-fresh runtime block (providers, models, secrets flags, heartbeat...)."""
        self.store = store
        self.runtime = runtime
        self.cache = {}                 # (kind, id) -> (stamp, serialized slim record)
        self.lock = threading.Lock()
        self.memo = {}                  # (since, rev, runtime) -> response, so repeated polls and tabs cost nothing
        self.stats = {"full_builds": 0, "delta_builds": 0, "hits": 0}

    # ---- records ---------------------------------------------------------------------------------------------------
    def _text(self, kind, record_id, payload, version, stamp):
        key = (kind, record_id)
        hit = self.cache.get(key)
        if hit and hit[0] == (version, stamp):
            return hit[1]
        if kind in PROJECTED:
            text = json.dumps({**project(kind, json.loads(payload)), "version": version}, ensure_ascii=False)
        else:
            text = (payload[:-1] + (',' if len(payload) > 2 else '') + '"version":%d}' % version) if payload.endswith("}") else json.dumps({**json.loads(payload), "version": version}, ensure_ascii=False)
        self.cache[key] = ((version, stamp), text)
        return text

    def _stamps(self):
        with self.store.lock:
            return dict(self.store.changed)

    def _collection(self, kind):
        """JSON array text of a whole collection (newest tool events only; the message window is built separately)."""
        store = self.store
        with store.lock:
            if kind == "tool_events":
                rows = [(i, p, v) for _, i, p, v in store.latest_rows(kind, TOOL_EVENTS_LATEST)]
            else:
                rows = store.conn.execute("SELECT id,payload,version FROM records WHERE kind=? ORDER BY rowid", (kind,)).fetchall()
            stamps = {k: n for k, n in store.changed.items() if k[0] == kind}
        return "[" + ",".join(self._text(kind, i, p, v, stamps.get((kind, i), 0)) for i, p, v in rows) + "]"

    def _messages(self):
        store = self.store
        with store.lock:
            open_ids = [d["id"] for d in store.all("decisions") if d.get("status") == "open"]
            rows, total, first, older = store.message_window(MESSAGES_PER_CHANNEL, open_ids)
            stamps = {k: n for k, n in store.changed.items() if k[0] == "messages"}
        return "[" + ",".join(self._text("messages", i, p, v, stamps.get(("messages", i), 0)) for _, i, p, v in rows) + "]", {
            "shown": len(rows), "total": total, "per_channel": MESSAGES_PER_CHANNEL, "first": first, "older": older}

    # ---- responses -------------------------------------------------------------------------------------------------
    def _wrap(self, head, runtime_json):
        """Finish a response: serialize once, gzip once, hash once."""
        raw = (head + ',"runtime":' + runtime_json + "}").encode()
        return {"raw": raw, "gzip": gz(raw), "etag": '"%s"' % hashlib.sha1(raw).hexdigest()[:20]}

    def respond(self, since=None):
        """{raw, gzip, etag, full} for GET /api/state[?since=<rev>]; a cursor from another process or a missing one gives the full state."""
        store = self.store
        runtime = self.runtime()
        # `server_time` changes on every call and the page does not read it: leaving it out lets an unchanged state answer 304.
        runtime = {**runtime, "heartbeat": {k: v for k, v in (runtime.get("heartbeat") or {}).items() if k != "server_time"}} if "heartbeat" in runtime else runtime
        runtime_json = json.dumps(runtime, ensure_ascii=False)
        with self.lock:
            seq = store.parse_cursor(since) if since else None
            full = seq is None
            cursor = store.cursor()
            key = ("full" if full else seq, cursor, runtime_json)
            hit = self.memo.get(key)
            if hit:
                self.stats["hits"] += 1
                return hit
            response = {**(self._full(cursor, runtime_json) if full else self._delta(cursor, seq, runtime_json)), "full": full}
            if len(self.memo) > 6:
                self.memo.clear()
            self.memo[key] = response
            return response

    def _full(self, cursor, runtime_json):
        # The rev was read before the rows: a write in between is re-sent by the next delta instead of being lost.
        store = self.store
        self.stats["full_builds"] += 1
        parts, meta = [], {}
        for kind in KINDS:
            if kind in OMITTED and kind != "tool_events":
                continue
            if kind == "messages":
                text, meta["messages"] = self._messages()
            else:
                text = self._collection(kind)
            parts.append('"%s":%s' % (kind, text))
        head = '{"rev":"%s","full":true,%s,"settings":%s,"meta":%s' % (cursor, ",".join(parts), json.dumps(store.settings(), ensure_ascii=False), json.dumps(meta))
        return self._wrap(head, runtime_json)

    def _delta(self, cursor, seq, runtime_json):
        store = self.store
        self.stats["delta_builds"] += 1
        rows, deleted, settings_changed = store.rows_since(seq)
        stamps = self._stamps()
        changes, drops = {}, {}
        for _, kind, record_id, payload, version in rows:
            if kind not in OMITTED:
                changes.setdefault(kind, []).append(self._text(kind, record_id, payload, version, stamps.get((kind, record_id), 0)))
        for kind, record_id in deleted:
            self.cache.pop((kind, record_id), None)
            if kind not in OMITTED:
                drops.setdefault(kind, []).append(record_id)
        head = '{"rev":"%s","full":false,"changes":{%s},"deleted":%s' % (
            cursor, ",".join('"%s":[%s]' % (k, ",".join(v)) for k, v in changes.items()), json.dumps(drops))
        if settings_changed:
            head += ',"settings":' + json.dumps(store.settings(), ensure_ascii=False)
        return self._wrap(head, runtime_json)

    # ---- on-demand pieces ------------------------------------------------------------------------------------------
    def run_events(self, run_id, limit=200):
        """Full tool events of one run, oldest first (the state only carries clipped copies of the newest few)."""
        store = self.store
        limit = max(1, min(int(limit), 1000))
        with store.lock:
            try:
                rows = store.conn.execute("SELECT payload,version FROM records WHERE kind='tool_events' AND json_extract(payload,'$.run_id')=? ORDER BY rowid DESC LIMIT ?",
                                          (run_id, limit)).fetchall()
            except Exception:
                rows = [(p, v) for p, v in store.conn.execute("SELECT payload,version FROM records WHERE kind='tool_events' ORDER BY rowid DESC").fetchall()
                        if json.loads(p).get("run_id") == run_id][:limit]
        return [{**json.loads(p), "version": v} for p, v in rows[::-1]]
