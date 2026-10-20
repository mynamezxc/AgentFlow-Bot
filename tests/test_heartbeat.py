import copy
import http.client
import json
import threading
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from agentflow.engine import Engine
from agentflow.heartbeat import DEFAULT_PULSE_PROMPT, DEFAULTS, active_at, next_window_at
from agentflow.providers import Providers, Vault
from agentflow.service import Service
from agentflow.store import DomainError, Store
from server import make_handler

ROOT = Path(__file__).resolve().parents[1]
SEED = json.loads((ROOT / "dist" / "seed.json").read_text(encoding="utf-8"))
UTC = timezone.utc


def vn(day, hour, minute=0, month=10):
    """A GMT+7 wall-clock time as an aware datetime (2026-10-19 is a Monday)."""
    return datetime(2026, month, day, hour, minute, tzinfo=timezone(timedelta(hours=7)))


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "data", copy.deepcopy(SEED))
        self.vault = Vault(self.root / "data")
        self.providers = Providers(self.store, self.vault, self.root / "company")
        self.providers.cli = {name: None for name in ("codex", "claude", "gemini")}
        self.account_patch = patch.object(self.providers, "read_account", return_value={"type": "test", "label": "Unit account"})
        self.account_patch.start()
        self.engine = Engine(self.store, self.providers, self.vault, self.root / "company")
        self.service = Service(self.store, self.providers, self.vault, self.engine)
        for agent in ("a-lead", "a-seo"):
            self.store.patch("agents", agent, {"provider": "gateway", "model": "observed-test-model"})

    def tearDown(self):
        self.engine.close()
        self.account_patch.stop()
        self.store.close()
        self.temp.cleanup()

    def configure(self, **changes):
        self.service.settings({"heartbeat": changes})

    def task(self, status="ready", **extra):
        return self.store.create("tasks", {"title": "Write the launch brief", "agent_id": "a-seo", "reviewer_id": "a-review",
                                           "status": status, "priority": "medium", **extra})

    def pulse_runs(self):
        return [r for r in self.store.all("runs") if r.get("system_pulse")]

    # ---- settings -----------------------------------------------------------------------------------------------
    def test_defaults_apply_when_missing(self):
        cfg = self.engine.heartbeat.config()
        self.assertEqual(cfg["interval_seconds"], 30)
        self.assertEqual(cfg["active_days"], [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(cfg["active_hours"], {"enabled": False, "start": "08:00", "end": "22:00"})
        self.assertTrue(cfg["pause_schedules_outside_hours"])
        self.assertEqual(cfg["pulse"], {"enabled": False, "agent_id": "a-lead", "every_minutes": 60, "only_if_changed": True,
                                        "notify": "needed", "prompt": DEFAULT_PULSE_PROMPT})
        self.assertEqual(cfg, json.loads(json.dumps(DEFAULTS)))

    def test_settings_validation_and_partial_merge(self):
        saved = self.service.settings({"heartbeat": {"interval_seconds": 60, "pulse": {"enabled": True, "every_minutes": 30}},
                                       "max_parallel": 2, "turns_per_slice": 9})["heartbeat"]
        self.assertEqual(saved["interval_seconds"], 60)
        self.assertEqual(saved["pulse"]["every_minutes"], 30)
        self.assertEqual(saved["pulse"]["agent_id"], "a-lead")  # untouched fields keep their value
        self.service.settings({"heartbeat": {"active_hours": {"enabled": True}}})
        merged = self.engine.heartbeat.config()
        self.assertTrue(merged["active_hours"]["enabled"])
        self.assertEqual(merged["active_hours"]["start"], "08:00")
        self.assertEqual(merged["pulse"]["every_minutes"], 30)
        bad = [{"interval_seconds": 9}, {"interval_seconds": 301}, {"interval_seconds": True}, {"interval_seconds": "30"},
               {"active_hours": {"start": "8:00"}}, {"active_hours": {"end": "24:00"}}, {"active_hours": {"enabled": "yes"}},
               {"active_days": []}, {"active_days": [0]}, {"active_days": [8]}, {"active_days": "all"},
               {"pause_schedules_outside_hours": 1}, {"unknown": 1}, {"pulse": {"every_minutes": 14}},
               {"pulse": {"every_minutes": 1441}}, {"pulse": {"notify": "never"}}, {"pulse": {"agent_id": "a-missing"}},
               {"pulse": {"prompt": "   "}}, {"pulse": {"prompt": "Hãy kiểm tra công việc của nhóm và báo cáo cho chủ sở hữu ngay"}},
               {"pulse": {"enabled": "on"}}, {"pulse": {"extra": 1}}, "fast"]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(DomainError):
                self.service.settings({"heartbeat": payload})
        self.assertEqual(self.engine.heartbeat.config(), merged)  # rejected payloads change nothing
        self.assertEqual(self.store.settings()["max_parallel"], 2)
        self.assertEqual(self.store.settings()["turns_per_slice"], 9)

    def test_archived_agent_cannot_run_the_pulse(self):
        self.store.patch("agents", "a-social", {"archived": True})
        with self.assertRaises(DomainError):
            self.configure(pulse={"agent_id": "a-social"})

    def test_state_is_not_owner_writable(self):
        self.service.settings({"heartbeat_state": {"next_pulse_at": "2020-01-01T00:00:00Z"}})
        self.assertNotIn("heartbeat_state", self.store.settings())

    # ---- loop cadence -------------------------------------------------------------------------------------------
    def run_loop(self, iterations):
        clock = {"t": 0.0}
        waits = iter([False] * iterations + [True])

        def wait(_timeout):
            clock["t"] += 5.0
            return next(waits)

        with patch.object(self.engine.stop, "wait", side_effect=wait), patch("agentflow.runtime.time.monotonic", side_effect=lambda: clock["t"]), \
                patch.object(self.engine, "tick") as tick, patch.object(self.engine, "pump"):
            self.engine.loop()
        return tick.call_count

    def test_loop_uses_configured_interval(self):
        self.assertEqual(self.run_loop(7), 2)  # 30 s default: ticks at t=5 and t=35
        self.configure(interval_seconds=10)
        self.assertEqual(self.run_loop(7), 4)  # ticks at t=5, 15, 25, 35
        self.assertEqual(self.engine.heartbeat.interval(), 10)

    # ---- active window maths -------------------------------------------------------------------------------------
    def test_window_same_day_and_all_day(self):
        cfg = self.service.settings({"heartbeat": {"active_hours": {"enabled": True, "start": "08:00", "end": "22:00"}}})["heartbeat"]
        self.assertTrue(active_at(cfg, vn(19, 8, 0)))
        self.assertTrue(active_at(cfg, vn(19, 21, 59)))
        self.assertFalse(active_at(cfg, vn(19, 22, 0)))
        self.assertFalse(active_at(cfg, vn(19, 7, 59)))
        self.assertEqual(next_window_at(cfg, vn(19, 23, 0)), vn(20, 8, 0))
        self.assertEqual(next_window_at(cfg, vn(19, 6, 0)), vn(19, 8, 0))
        everyday = {**cfg, "active_hours": {"enabled": True, "start": "09:00", "end": "09:00"}}
        self.assertTrue(active_at(everyday, vn(19, 3, 0)))  # start == end means the whole day
        off = {**cfg, "active_hours": {**cfg["active_hours"], "enabled": False}}
        self.assertTrue(active_at(off, vn(19, 3, 0)))
        self.assertIsNone(next_window_at(off, vn(19, 3, 0)))

    def test_overnight_window_belongs_to_the_day_it_starts_on(self):
        cfg = self.service.settings({"heartbeat": {"active_hours": {"enabled": True, "start": "22:00", "end": "06:00"},
                                                   "active_days": [1, 2, 3, 4, 5]}})["heartbeat"]
        self.assertEqual(vn(19, 12).isoweekday(), 1)
        self.assertTrue(active_at(cfg, vn(19, 23, 0)))       # Monday night
        self.assertTrue(active_at(cfg, vn(20, 5, 59)))       # still Monday's window on Tuesday morning
        self.assertFalse(active_at(cfg, vn(20, 6, 0)))
        self.assertFalse(active_at(cfg, vn(19, 12, 0)))
        self.assertTrue(active_at(cfg, vn(24, 3, 0)))        # Saturday 03:00 belongs to Friday's window
        self.assertFalse(active_at(cfg, vn(24, 23, 0)))      # Saturday night is not allowed
        self.assertFalse(active_at(cfg, vn(26, 3, 0)))       # Monday 03:00 belongs to Sunday: not allowed
        self.assertEqual(next_window_at(cfg, vn(24, 10, 0)), vn(26, 22, 0))  # Saturday noon -> Monday 22:00
        self.assertEqual(next_window_at(cfg, vn(26, 3, 0)), vn(26, 22, 0))

    # ---- quiet hours for schedules -------------------------------------------------------------------------------
    def window_08_22(self, **extra):
        self.configure(active_hours={"enabled": True, "start": "08:00", "end": "22:00"}, **extra)

    def test_quiet_hours_skip_then_fire_once_at_window_open(self):
        self.window_08_22()
        self.store.patch("schedules", "s-1", {"enabled": True, "next_run": vn(19, 0, 0).astimezone(UTC).isoformat()})
        self.store.patch("schedules", "s-2", {"enabled": True, "next_run": vn(16, 9, 0).astimezone(UTC).isoformat()})  # days overdue
        with patch.object(self.providers, "infer") as inference:
            self.engine.tick(vn(19, 2, 0))
            self.engine.tick(vn(19, 5, 0))
            self.assertEqual(self.store.all("runs"), [])
            opening = vn(19, 8, 0).astimezone(UTC)
            for key in ("s-1", "s-2"):
                self.assertEqual(datetime.fromisoformat(self.store.get("schedules", key)["next_run"].replace("Z", "+00:00")), opening)
            self.engine.tick(vn(19, 7, 59))
            self.assertEqual(self.store.all("runs"), [])
            self.engine.tick(vn(19, 8, 0))
            self.engine.tick(vn(19, 8, 0))
            self.engine.tick(vn(19, 8, 1))
            inference.assert_not_called()
        runs = self.store.all("runs")
        self.assertEqual(sorted(r["schedule_id"] for r in runs), ["s-1", "s-2"])  # exactly one run per schedule, no backlog burst
        for key in ("s-1", "s-2"):
            self.assertGreater(datetime.fromisoformat(self.store.get("schedules", key)["next_run"].replace("Z", "+00:00")), vn(19, 8, 1))

    def test_schedules_run_in_quiet_hours_when_the_pause_is_off(self):
        self.window_08_22(pause_schedules_outside_hours=False)
        self.store.patch("schedules", "s-1", {"enabled": True, "next_run": vn(19, 0, 0).astimezone(UTC).isoformat()})
        self.engine.tick(vn(19, 2, 0))
        self.assertEqual(len(self.store.all("runs")), 1)

    def test_quiet_hours_never_pause_owner_chat_or_running_work(self):
        self.window_08_22()
        running = self.engine.enqueue("a-seo", "Keep going", "task")
        with patch.object(self.providers, "infer", return_value=json.dumps({"reply": "Done."})):
            self.engine.tick(vn(19, 2, 0))
            chat = self.engine.chat({"message": "Hello at night", "agent_id": "a-lead", "channel": "dm-a-lead"})
            futures = self.engine.pump()
            for future in futures:
                future.result(timeout=10)
        self.assertEqual({running["id"], chat["id"]}, {r["id"] for r in self.store.all("runs") if r["status"] == "done"})

    # ---- team pulse ----------------------------------------------------------------------------------------------
    def enable_pulse(self, **pulse):
        self.configure(pulse={"enabled": True, **pulse})

    def test_pulse_enqueues_one_run_with_a_digest_and_no_model_call(self):
        failed = self.task("failed", error="Provider timed out")
        self.task("ready")
        self.enable_pulse()
        at = datetime(2026, 10, 19, 3, 0, tzinfo=UTC)
        with patch.object(self.providers, "infer") as inference:
            self.engine.tick(at)
            self.engine.tick(at + timedelta(seconds=30))  # not due again
            inference.assert_not_called()
        runs = self.pulse_runs()
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual((run["kind"], run["agent_id"], run["channel"], run["status"]), ("schedule", "a-lead", "dm-a-lead", "queued"))
        self.assertTrue(run["system_pulse"])
        self.assertIn("Team pulse digest", run["prompt"])
        self.assertIn(failed["id"], run["prompt"])
        self.assertIn("Provider timed out", run["prompt"])
        self.assertIn("Pending owner decisions: 0", run["prompt"])
        self.assertIn("Notify mode: needed", run["prompt"])
        self.assertTrue(run["prompt"].startswith(DEFAULT_PULSE_PROMPT))
        state = self.engine.heartbeat.state()
        self.assertEqual((state["last_result"], state["last_run_id"]), ("started", run["id"]))
        self.assertEqual(state["next_pulse_at"], (at + timedelta(minutes=60)).isoformat().replace("+00:00", "Z"))

    def test_custom_prompt_and_notify_always_reach_the_run(self):
        self.task("ready")
        self.enable_pulse(notify="always", prompt="Check the team and report.")
        self.engine.tick(datetime(2026, 10, 19, 3, 0, tzinfo=UTC))
        prompt = self.pulse_runs()[0]["prompt"]
        self.assertTrue(prompt.startswith("Check the team and report."))
        self.assertIn("Notify mode: always", prompt)

    def test_pulse_skips_unchanged_digest_and_nothing_open_without_a_model_call(self):
        self.task("ready")
        self.enable_pulse()
        at = datetime(2026, 10, 19, 3, 0, tzinfo=UTC)
        with patch.object(self.providers, "infer") as inference:
            self.engine.tick(at)
            self.store.patch("runs", self.pulse_runs()[0]["id"], {"status": "done"})
            self.engine.tick(at + timedelta(minutes=61))
            state = self.engine.heartbeat.state()
            self.assertEqual((state["last_result"], state["last_reason"]), ("skipped", "unchanged"))
            self.assertEqual(len(self.pulse_runs()), 1)
            self.store.create("tasks", {"title": "Another", "agent_id": "a-seo", "reviewer_id": "a-review", "status": "ready"})
            self.engine.tick(at + timedelta(minutes=122))  # digest changed -> a new pulse
            self.assertEqual(len(self.pulse_runs()), 2)
            for run in self.pulse_runs():
                self.store.patch("runs", run["id"], {"status": "done"})
            for item in self.store.all("tasks"):
                if not item.get("sample"):
                    self.store.patch("tasks", item["id"], {"status": "done"})
            self.engine.tick(at + timedelta(minutes=190))
            state = self.engine.heartbeat.state()
            self.assertEqual((state["last_result"], state["last_reason"]), ("skipped", "nothing_open"))
            self.assertEqual(len(self.pulse_runs()), 2)
            inference.assert_not_called()

    def test_only_if_changed_off_pulses_every_time(self):
        self.task("ready")
        self.enable_pulse(only_if_changed=False)
        at = datetime(2026, 10, 19, 3, 0, tzinfo=UTC)
        self.engine.tick(at)
        self.store.patch("runs", self.pulse_runs()[0]["id"], {"status": "done"})
        self.engine.tick(at + timedelta(minutes=61))
        self.assertEqual(len(self.pulse_runs()), 2)

    def test_digest_covers_blocked_runs_stalls_failures_and_decisions(self):
        at = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)
        old = (at - timedelta(hours=5)).isoformat().replace("+00:00", "Z")
        dependency = self.task("failed", error="boom")
        self.task("ready", dependencies=[dependency["id"]], title="Blocked follow up")
        stalled = self.store.create("runs", {"agent_id": "a-seo", "prompt": "Long job", "kind": "task", "status": "running", "steps": 3})
        self.store.conn.execute("UPDATE records SET payload=json_set(payload,'$.updated_at',?) WHERE kind='runs' AND id=?", (old, stalled["id"]))
        self.store.create("runs", {"agent_id": "a-seo", "prompt": "Waiting", "kind": "chat", "status": "waiting_user"})
        self.store.create("runs", {"agent_id": "a-seo", "prompt": "Quota", "kind": "task", "status": "waiting_quota"})
        self.store.create("runs", {"agent_id": "a-seo", "prompt": "Broke", "kind": "task", "status": "failed", "error": "Model said no",
                                   "completed_at": (at - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")})
        self.store.create("decisions", {"question": "Pick one", "status": "open", "agent_id": "a-lead"})
        digest = self.engine.heartbeat.digest(self.engine.heartbeat.config(), at, at - timedelta(hours=1))
        text = digest["text"]
        self.assertTrue(digest["open"])
        for fragment in ("failed", "blocked", "Blocked follow up", "waiting owner", "waiting quota", "Stalled runs", stalled["id"],
                         "New failures since the last pulse", "Model said no", "Pending owner decisions: 1"):
            self.assertIn(fragment, text)
        self.assertLess(len(text), 2500)
        again = self.engine.heartbeat.digest(self.engine.heartbeat.config(), at + timedelta(minutes=1), at - timedelta(hours=1))
        self.assertEqual(digest["hash"], again["hash"])  # elapsed time alone never changes the hash

    def test_pulse_waits_for_active_hours_but_ignores_pause_toggle(self):
        self.task("ready")
        self.window_08_22(pause_schedules_outside_hours=False)
        self.enable_pulse()
        self.engine.tick(vn(19, 3, 0))
        self.assertEqual(self.pulse_runs(), [])
        self.engine.tick(vn(19, 9, 0))
        self.assertEqual(len(self.pulse_runs()), 1)

    def test_pulse_never_overlaps(self):
        self.task("ready")
        self.enable_pulse()
        at = datetime(2026, 10, 19, 3, 0, tzinfo=UTC)
        self.engine.tick(at)
        first = self.pulse_runs()[0]
        self.store.create("tasks", {"title": "More", "agent_id": "a-seo", "reviewer_id": "a-review", "status": "running"})
        for status in ("queued", "running", "waiting_user", "waiting_quota"):
            self.store.patch("runs", first["id"], {"status": status})
            self.engine.tick(at + timedelta(hours=3))
            self.assertEqual(len(self.pulse_runs()), 1, status)
            self.assertEqual(self.engine.heartbeat.pulse_now()["reason"], "running")
        self.store.patch("runs", first["id"], {"status": "done"})
        self.engine.tick(at + timedelta(hours=3))
        self.assertEqual(len(self.pulse_runs()), 2)

    def test_pulse_now_ignores_only_if_changed_and_disabled_state(self):
        self.task("ready")
        result = self.engine.heartbeat.pulse_now()  # pulse is disabled, the owner still asked for it
        self.assertTrue(result["started"])
        self.assertEqual(len(self.pulse_runs()), 1)
        self.store.patch("runs", result["run_id"], {"status": "done"})
        self.enable_pulse()
        self.engine.tick(datetime.now(UTC))  # unchanged digest -> skipped by the schedule
        self.assertEqual(len(self.pulse_runs()), 1)
        again = self.engine.heartbeat.pulse_now()
        self.assertTrue(again["started"])
        self.assertEqual(len(self.pulse_runs()), 2)
        self.assertIn("pulse", again["status"])

    def test_pulse_now_skips_when_nothing_is_open_and_needs_execution(self):
        with patch.object(self.providers, "infer") as inference:
            result = self.engine.heartbeat.pulse_now()
            inference.assert_not_called()
        self.assertEqual((result["started"], result["reason"]), (False, "nothing_open"))
        self.store.settings({"execution_enabled": False})
        with self.assertRaises(DomainError):
            self.engine.heartbeat.pulse_now()

    def test_paused_pulse_agent_is_skipped_with_a_reason(self):
        self.task("ready")
        self.store.patch("agents", "a-lead", {"status": "paused"})
        self.assertEqual(self.engine.heartbeat.pulse_now()["reason"], "agent_unavailable")
        self.assertEqual(self.pulse_runs(), [])

    def test_execution_disabled_stops_schedules_and_pulse(self):
        self.task("ready")
        self.enable_pulse()
        self.store.settings({"execution_enabled": False})
        self.engine.tick(datetime(2026, 10, 19, 3, 0, tzinfo=UTC))
        self.assertEqual(self.pulse_runs(), [])

    def test_silent_pulse_reply_and_no_episode_memory(self):
        self.task("ready")
        self.enable_pulse()
        self.engine.tick(datetime(2026, 10, 19, 3, 0, tzinfo=UTC))
        run = self.pulse_runs()[0]
        episodes = len([m for m in self.store.all("memories") if m.get("type") == "episode"])
        with patch.object(self.providers, "infer", return_value=json.dumps({"reply": "All quiet.", "summary": "Nothing to unblock."})):
            self.engine.execute(run["id"])
        self.assertEqual(self.store.get("runs", run["id"])["status"], "done")
        messages = [m for m in self.store.all("messages") if m.get("run_id") == run["id"]]
        self.assertTrue(messages)
        self.assertTrue(all(m["audience"] == "activity" for m in messages))
        self.assertEqual(len([m for m in self.store.all("memories") if m.get("type") == "episode"]), episodes)

    def test_notify_always_reports_to_the_owner(self):
        self.task("ready")
        self.enable_pulse(notify="always")
        self.engine.tick(datetime(2026, 10, 19, 3, 0, tzinfo=UTC))
        run = self.pulse_runs()[0]
        with patch.object(self.providers, "infer", return_value=json.dumps({"reply": "Status: all moving."})):
            self.engine.execute(run["id"])
        messages = [m for m in self.store.all("messages") if m.get("run_id") == run["id"]]
        self.assertEqual([m["audience"] for m in messages], ["owner"])

    # ---- status payload -----------------------------------------------------------------------------------------
    def test_status_payload(self):
        for status in ("queued", "queued", "running", "waiting_user", "blocked_model", "waiting_quota", "done"):
            self.store.create("runs", {"agent_id": "a-seo", "prompt": "x", "kind": "task", "status": status})
        self.engine.tick(datetime.now(UTC))
        status = self.engine.heartbeat_status()
        self.assertEqual((status["queued"], status["running"], status["waiting_owner"], status["waiting_quota"]), (2, 1, 2, 1))
        self.assertEqual(status["interval_seconds"], 30)
        self.assertTrue(status["active_now"])
        self.assertIsNone(status["next_window_at"])
        self.assertTrue(status["last_heartbeat_at"])
        self.assertEqual(set(status["pulse"]), {"enabled", "agent_id", "every_minutes", "last_pulse_at", "last_result", "last_reason", "next_pulse_at"})
        self.assertFalse(status["pulse"]["enabled"])
        self.assertIsNone(status["pulse"]["next_pulse_at"])

    def test_status_reports_quiet_hours_and_next_window(self):
        self.window_08_22()
        with patch("agentflow.heartbeat.datetime") as clock:
            clock.now.return_value = vn(19, 23, 30).astimezone(UTC)
            status = self.engine.heartbeat.status()
        self.assertFalse(status["active_now"])
        self.assertTrue(status["quiet_hours"])
        self.assertEqual(datetime.fromisoformat(status["next_window_at"].replace("Z", "+00:00")), vn(20, 8, 0))

    def test_status_tracks_the_pulse_run_result(self):
        self.task("ready")
        self.enable_pulse()
        self.engine.tick(datetime(2026, 10, 19, 3, 0, tzinfo=UTC))
        run = self.pulse_runs()[0]
        self.assertEqual(self.engine.heartbeat_status()["pulse"]["last_result"], "running")
        self.store.patch("runs", run["id"], {"status": "done"})
        self.assertEqual(self.engine.heartbeat_status()["pulse"]["last_result"], "done")
        self.assertTrue(self.engine.heartbeat_status()["pulse"]["next_pulse_at"])

    def test_enabling_the_pulse_rearms_it(self):
        self.engine.heartbeat.remember(next_pulse_at="2099-01-01T00:00:00Z")
        self.enable_pulse()
        self.assertIsNone(self.engine.heartbeat.state()["next_pulse_at"])

    # ---- HTTP + UI contract ---------------------------------------------------------------------------------------
    def test_http_snapshot_and_pulse_now_endpoint(self):
        self.task("ready")
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service, "session-token", 0))
        port = server.server_port
        server.RequestHandlerClass = make_handler(self.service, "session-token", port)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        conn = http.client.HTTPConnection("127.0.0.1", port)
        headers = {"Content-Type": "application/json", "Cookie": "af_session=session-token", "Origin": f"http://127.0.0.1:{port}"}
        try:
            conn.request("GET", "/api/state", headers={"Cookie": "af_session=session-token"})
            runtime = json.loads(conn.getresponse().read())["runtime"]
            self.assertEqual(runtime["heartbeat"]["interval_seconds"], 30)
            self.assertEqual(runtime["heartbeat_config"]["pulse"]["agent_id"], "a-lead")
            self.assertEqual(runtime["heartbeat_defaults"]["pulse"]["prompt"], DEFAULT_PULSE_PROMPT)
            conn.request("POST", "/api/heartbeat/pulse-now", "{}", {**headers, "Origin": "https://evil.example"})
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            conn.request("POST", "/api/heartbeat/pulse-now", "{}", headers)
            response = conn.getresponse()
            result = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertTrue(result["started"])
            self.assertEqual(result["status"]["pulse"]["last_result"], "running")
            conn.request("POST", "/api/heartbeat/pulse-now", "{}", headers)
            self.assertEqual(json.loads(conn.getresponse().read())["reason"], "running")
            conn.request("POST", "/api/settings", json.dumps({"heartbeat": {"interval_seconds": 5}}), headers)
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, 400)
        finally:
            conn.close()
            server.shutdown()
            server.server_close()

    def test_ui_default_prompt_mirrors_the_runtime_default(self):
        source = (ROOT / "dist" / "heartbeat-ui.js").read_text(encoding="utf-8")
        literal = source.split("DEFAULT_PULSE_PROMPT=`", 1)[1].split("`;", 1)[0].replace("\\`", "`")
        self.assertEqual(literal, DEFAULT_PULSE_PROMPT)


if __name__ == "__main__":
    unittest.main()
