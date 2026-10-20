"""The slim, incremental /api/state: change feed, projection, message window, gzip/304 and paging endpoints."""
import copy
import gzip
import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from agentflow import statefeed
from agentflow.engine import Engine
from agentflow.providers import Providers, Vault
from agentflow.service import Service
from agentflow.statefeed import StateFeed
from agentflow.store import DomainError, Store
from server import make_handler

ROOT = Path(__file__).resolve().parents[1]
SEED = json.loads((ROOT / "dist" / "seed.json").read_text(encoding="utf-8"))
COOKIE = {"Cookie": "af_session=session-token"}


def parse(response):
    return json.loads(response["raw"])


class FeedBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "data", copy.deepcopy(SEED))
        self.feed = StateFeed(self.store, lambda: {"mode": "local", "heartbeat": {"server_time": "x", "queued": 0}, "providers": []})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def run_record(self, **extra):
        return self.store.create("runs", {"agent_id": "a-lead", "status": "done", "prompt": "p", "channel": "dm-a-lead",
                                          "trace": [{"role": "assistant", "content": "x" * 500}], "native_session": {"id": "s"},
                                          "model_history": [1, 2, 3], "pending_calls": [], "operation_fingerprints": {"a": 1}, **extra})

    def message(self, channel, text="hi", **extra):
        return self.store.create("messages", {"channel": channel, "role": "assistant", "content": text, "agent_id": "a-lead", **extra})


class ChangeFeedTests(FeedBase):
    def test_delta_reports_create_patch_and_delete_since_the_cursor(self):
        first = parse(self.feed.respond())
        self.assertTrue(first["full"])
        note = self.store.create("memories", {"title": "A", "content": "one", "scope": "shared"})
        other = self.store.create("memories", {"title": "B", "content": "two", "scope": "shared"})
        delta = parse(self.feed.respond(first["rev"]))
        self.assertFalse(delta["full"])
        self.assertEqual({m["id"] for m in delta["changes"]["memories"]}, {note["id"], other["id"]})
        self.assertEqual(delta["changes"]["memories"][0]["version"], 1)
        self.store.patch("memories", note["id"], {"title": "A2"})
        patched = parse(self.feed.respond(delta["rev"]))
        self.assertEqual([m["title"] for m in patched["changes"]["memories"]], ["A2"])
        self.assertEqual(patched["changes"]["memories"][0]["version"], 2)
        self.store.delete("memories", other["id"])
        gone = parse(self.feed.respond(patched["rev"]))
        self.assertEqual(gone["deleted"], {"memories": [other["id"]]})
        self.assertEqual(gone["changes"], {})

    def test_nothing_changed_gives_an_empty_delta_and_a_stable_etag(self):
        first = parse(self.feed.respond())
        a = self.feed.respond(first["rev"])
        b = self.feed.respond(first["rev"])
        self.assertEqual(parse(a)["changes"], {})
        self.assertEqual(a["etag"], b["etag"])
        self.store.create("memories", {"title": "n", "content": "c", "scope": "shared"})
        self.assertNotEqual(self.feed.respond(first["rev"])["etag"], a["etag"])

    def test_unknown_or_foreign_cursor_returns_the_full_state(self):
        self.assertTrue(parse(self.feed.respond("deadbeef.5"))["full"])
        self.assertTrue(parse(self.feed.respond("garbage"))["full"])
        self.assertTrue(parse(self.feed.respond(f"{self.store.epoch}.999999"))["full"])   # from the future
        reopened = Store(self.root / "data", copy.deepcopy(SEED))   # another process: another epoch
        try:
            self.assertNotEqual(reopened.epoch, self.store.epoch)
            self.assertTrue(parse(self.feed.respond(reopened.cursor()))["full"])
        finally:
            reopened.close()

    def test_direct_table_updates_are_picked_up_after_mark_changed(self):
        note = self.store.create("memories", {"title": "Direct", "content": "old", "scope": "shared"})
        rev = parse(self.feed.respond())["rev"]
        payload = json.loads(self.store.conn.execute("SELECT payload FROM records WHERE kind='memories' AND id=?", (note["id"],)).fetchone()[0])
        payload["content"] = "trimmed"
        self.store.conn.execute("UPDATE records SET payload=? WHERE kind='memories' AND id=?", (json.dumps(payload), note["id"]))
        self.store.mark_changed("memories", note["id"])   # what housekeeping does: the version does not change
        delta = parse(self.feed.respond(rev))
        self.assertEqual(delta["changes"]["memories"][0]["content"], "trimmed")

    def test_settings_travel_only_when_they_changed(self):
        rev = parse(self.feed.respond())["rev"]
        self.assertNotIn("settings", parse(self.feed.respond(rev)))
        self.store.settings({"workspace_name": "Renamed"})
        delta = parse(self.feed.respond(rev))
        self.assertEqual(delta["settings"]["workspace_name"], "Renamed")


class ProjectionTests(FeedBase):
    def test_full_state_is_slim_but_export_snapshot_is_not(self):
        run = self.run_record()
        for i in range(60):
            self.store.create("tool_events", {"run_id": run["id"], "name": "t%d" % i, "arguments": "a" * 2000, "result": "r" * 5000, "status": "done"})
        self.store.create("channel_outbox", {"content": "x", "status": "sent"})
        state = parse(self.feed.respond())
        sent = next(r for r in state["runs"] if r["id"] == run["id"])
        for hidden in statefeed.RUN_HIDDEN:
            self.assertNotIn(hidden, sent)
        self.assertEqual(sent["prompt"], "p")
        for omitted in ("channel_outbox", "model_events", "mcp_servers", "skill_versions", "agent_revisions"):
            self.assertNotIn(omitted, state)
        events = state["tool_events"]
        self.assertEqual(len(events), statefeed.TOOL_EVENTS_LATEST)
        self.assertEqual(events[-1]["name"], "t59")
        self.assertLessEqual(len(events[-1]["result"]), statefeed.TOOL_TEXT + 3)
        self.assertTrue(events[-1]["clipped"])
        full = self.store.snapshot()   # /api/export: nothing is slimmed
        self.assertIn("trace", next(r for r in full["runs"] if r["id"] == run["id"]))
        self.assertEqual(len(full["tool_events"]), 60)
        self.assertEqual(len(full["tool_events"][-1]["result"]), 5000)

    def test_delta_applies_the_same_projection_and_skips_omitted_kinds(self):
        rev = parse(self.feed.respond())["rev"]
        run = self.run_record()
        self.store.create("tool_events", {"run_id": run["id"], "name": "x", "result": "r", "arguments": "{}"})
        self.store.create("channel_outbox", {"content": "x"})
        delta = parse(self.feed.respond(rev))
        self.assertNotIn("trace", delta["changes"]["runs"][0])
        self.assertEqual(set(delta["changes"]), {"runs"})

    def test_attachment_text_and_file_change_bodies_stay_out(self):
        self.store.create("attachments", {"name": "a.txt", "text": "secret body", "mime": "text/plain"})
        self.store.create("file_changes", {"path": "a.txt", "before": "b" * 100, "after": "a" * 100})
        state = parse(self.feed.respond())
        self.assertNotIn("text", state["attachments"][0])
        self.assertNotIn("after", state["file_changes"][0])

    def test_run_events_endpoint_returns_the_full_events_of_one_run(self):
        run = self.run_record()
        other = self.run_record()
        for i in range(5):
            self.store.create("tool_events", {"run_id": run["id"], "name": "n%d" % i, "result": "r" * 3000})
        self.store.create("tool_events", {"run_id": other["id"], "name": "o", "result": "x"})
        events = self.feed.run_events(run["id"])
        self.assertEqual([e["name"] for e in events], ["n0", "n1", "n2", "n3", "n4"])
        self.assertEqual(len(events[0]["result"]), 3000)
        self.assertEqual(len(self.feed.run_events(run["id"], 2)), 2)

    def test_unchanged_records_are_serialized_once(self):
        self.run_record()
        parse(self.feed.respond())
        self.assertEqual(self.feed.stats["full_builds"], 1)
        self.store.create("memories", {"title": "n", "content": "c", "scope": "shared"})
        with patch.object(statefeed, "project", side_effect=AssertionError("re-projected an unchanged record")):
            parse(self.feed.respond())


class MessageWindowTests(FeedBase):
    def test_window_keeps_newest_per_channel_and_open_decision_messages(self):
        with patch.object(statefeed, "MESSAGES_PER_CHANNEL", 5):
            old = [self.message("dm-a", "a%d" % i) for i in range(12)]
            self.message("dm-b", "only-b")
            decision = self.store.create("decisions", {"question": "ok?", "status": "open", "agent_id": "a-lead", "channel": "dm-a"})
            kept = self.message("dm-a", "ask", kind="decision", decision_id=decision["id"])
            for i in range(8):
                self.message("dm-a", "c%d" % i)
            state = parse(self.feed.respond())
        texts = [m["content"] for m in state["messages"] if m["channel"] == "dm-a"]
        self.assertEqual(texts, ["ask", "c3", "c4", "c5", "c6", "c7"])   # the 5 newest plus the open decision's message
        self.assertIn("only-b", [m["content"] for m in state["messages"]])
        info = state["meta"]["messages"]
        self.assertEqual(info["older"], ["dm-a"])
        self.assertEqual(info["first"]["dm-a"], next(m["id"] for m in state["messages"] if m["content"] == "c3"))
        self.assertEqual(info["total"], len(self.store.all("messages")))

    def test_paging_returns_older_messages_oldest_first(self):
        made = [self.message("dm-p", "m%d" % i) for i in range(10)]
        self.message("dm-q", "other")
        page, more = self.store.messages_page("dm-p", made[7]["id"], 3)
        self.assertEqual([m["content"] for m in page], ["m4", "m5", "m6"])
        self.assertTrue(more)
        page, more = self.store.messages_page("dm-p", made[3]["id"], 10)
        self.assertEqual([m["content"] for m in page], ["m0", "m1", "m2"])
        self.assertFalse(more)
        newest, more = self.store.messages_page("dm-p", None, 4)
        self.assertEqual([m["content"] for m in newest], ["m6", "m7", "m8", "m9"])
        with self.assertRaises(DomainError):
            self.store.messages_page("dm-p", "m-missing", 3)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = Store(root / "data", copy.deepcopy(SEED))
        vault = Vault(root / "data")
        providers = Providers(self.store, vault, root / "company")
        providers.cli = {name: None for name in ("codex", "claude", "gemini")}
        self.account_patch = patch.object(providers, "read_account", return_value={"type": "test", "label": "Unit account"})
        self.account_patch.start()
        self.engine = Engine(self.store, providers, vault, root / "company")
        service = Service(self.store, providers, vault, self.engine)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service, "session-token", 0))
        self.port = self.server.server_port
        self.server.RequestHandlerClass = make_handler(service, "session-token", self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.engine.close()
        self.account_patch.stop()
        self.store.close()
        self.temp.cleanup()

    def get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        try:
            conn.request("GET", path, headers={**COOKIE, **(headers or {})})
            response = conn.getresponse()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        finally:
            conn.close()

    def test_state_is_gzipped_only_when_accepted_and_revalidates_with_etag(self):
        status, headers, plain = self.get("/api/state")
        self.assertEqual(status, 200)
        self.assertNotIn("content-encoding", headers)
        status, headers, packed = self.get("/api/state", {"Accept-Encoding": "gzip, deflate"})
        self.assertEqual(headers["content-encoding"], "gzip")
        self.assertEqual(headers["vary"], "Accept-Encoding")
        self.assertEqual(json.loads(gzip.decompress(packed))["rev"], json.loads(plain)["rev"])
        self.assertLess(len(packed), len(plain))
        self.assertEqual(self.get("/api/state", {"Accept-Encoding": "gzip;q=0"})[1].get("content-encoding"), None)
        etag = headers["etag"]
        status, headers304, body = self.get("/api/state", {"Accept-Encoding": "gzip", "If-None-Match": etag})
        self.assertEqual((status, body), (304, b""))
        self.assertEqual(headers304["etag"], etag)
        self.assertEqual(self.get("/api/state", {"If-None-Match": "*"})[0], 304)
        self.store.create("memories", {"title": "n", "content": "c", "scope": "shared"})
        self.assertEqual(self.get("/api/state", {"Accept-Encoding": "gzip", "If-None-Match": etag})[0], 200)

    def test_since_returns_only_what_changed(self):
        rev = json.loads(self.get("/api/state")[2])["rev"]
        note = self.store.create("memories", {"title": "Delta", "content": "c", "scope": "shared"})
        status, _, body = self.get("/api/state?since=" + rev, {"Accept-Encoding": "gzip"})
        data = json.loads(gzip.decompress(body))
        self.assertFalse(data["full"])
        self.assertEqual([m["id"] for m in data["changes"]["memories"]], [note["id"]])
        self.assertNotIn("agents", data)
        self.assertLess(len(body), 20000)
        self.assertTrue(json.loads(self.get("/api/state?since=nope.1")[2])["full"])

    def test_static_assets_validate_and_index_always_sets_the_cookie(self):
        status, headers, _ = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("af_session=session-token", headers["set-cookie"])
        self.assertNotIn("etag", headers)
        status, headers, _ = self.get("/", {"If-None-Match": "*"})
        self.assertEqual(status, 200)
        self.assertIn("set-cookie", headers)
        status, headers, body = self.get("/app.js", {"Accept-Encoding": "gzip"})
        self.assertEqual((status, headers["content-encoding"], headers["cache-control"]), (200, "gzip", "no-cache"))
        self.assertIn(b"state-sync.js", gzip.decompress(body))
        self.assertEqual(self.get("/app.js", {"If-None-Match": headers["etag"]})[0], 304)
        self.assertEqual(self.get("/app.js", {"If-None-Match": '"stale"'})[0], 200)

    def test_messages_endpoint_pages_and_validates(self):
        made = [self.store.create("messages", {"channel": "dm-a-lead", "role": "assistant", "content": "m%d" % i, "agent_id": "a-lead"}) for i in range(7)]
        status, _, body = self.get("/api/messages?channel=dm-a-lead&before=%s&limit=3" % made[5]["id"])
        page = json.loads(body)
        self.assertEqual([m["content"] for m in page["messages"]], ["m2", "m3", "m4"])
        self.assertTrue(page["more"])
        self.assertEqual(self.get("/api/messages")[0], 400)
        self.assertEqual(self.get("/api/messages?channel=dm-a-lead&before=missing")[0], 404)
        self.assertEqual(self.get("/api/messages?channel=dm-a-lead", {"Cookie": ""})[0], 401)

    def test_run_events_endpoint_and_full_export(self):
        run = self.store.create("runs", {"agent_id": "a-lead", "status": "done", "prompt": "p", "trace": [{"x": 1}], "channel": "dm-a-lead"})
        self.store.create("tool_events", {"run_id": run["id"], "name": "files.read", "result": "r" * 4000, "arguments": "{}"})
        status, _, body = self.get("/api/runs/%s/events" % run["id"])
        events = json.loads(body)["events"]
        self.assertEqual((status, len(events), len(events[0]["result"])), (200, 1, 4000))
        self.assertEqual(self.get("/api/runs/run-missing/events")[0], 404)
        export = json.loads(self.get("/api/export")[2])
        exported = next(r for r in export["runs"] if r["id"] == run["id"])
        self.assertEqual(exported["trace"], [{"x": 1}])
        self.assertEqual(len(export["tool_events"][-1]["result"]), 4000)
        state = json.loads(self.get("/api/state")[2])
        self.assertNotIn("trace", next(r for r in state["runs"] if r["id"] == run["id"]))
        self.assertEqual(self.get("/api/export", {"Cookie": ""})[0], 401)


if __name__ == "__main__":
    unittest.main()
