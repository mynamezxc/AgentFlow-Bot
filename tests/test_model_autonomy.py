import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

import test_runtime as runtime
from agentflow.store import DomainError
from server import make_handler
from test_quota_budget import QuotaBase


def models():
    return [{"id": "small", "provider": "codex", "name": "Small"},
            {"id": "large", "provider": "codex", "name": "Large"},
            {"id": "opus", "provider": "claude", "name": "Opus"},
            {"id": "sonnet", "provider": "claude", "name": "Sonnet"}]


PHASES = {"analysis": {"provider": "claude", "model": "opus"},
          "planning": {"provider": "claude", "model": "opus"},
          "coding": {"provider": "claude", "model": "sonnet"}}


class AutonomyBase(unittest.TestCase):
    def setUp(self):
        runtime.RuntimeTests.setUp(self)
        self.providers.models = models()
        self.engine.models.set_default({"provider": "codex", "model": "small"})
        self.run = self.engine.chat({"agent_id": "a-lead", "message": "Build a team"})

    tearDown = runtime.RuntimeTests.tearDown

    def mode(self, value):
        self.store.settings({"model_autonomy": value})

    def tool(self, name, args, actor="a-lead", run=None):
        return self.engine.tool(self.store.get("agents", actor), {"name": name, "arguments": args}, run or self.run)

    def agent(self, agent_id):
        return self.store.get("agents", agent_id)

    def events(self, **match):
        return [e for e in self.store.all("model_events") if all(e.get(k) == v for k, v in match.items())]

    def notes(self):
        return [m for m in self.store.all("messages") if m.get("audience") == "activity" and m["kind"] == "progress"
                and m["channel"] == "dm-a-lead" and "Team default model" in m["content"]]


class AutonomyModeTests(AutonomyBase):
    def test_phases_is_the_default_and_settings_validate_it(self):
        self.assertEqual(self.engine.models.autonomy(), "phases")
        with self.assertRaises(DomainError):
            self.service.settings({"model_autonomy": "wild"})
        self.service.settings({"model_autonomy": "locked"})
        self.assertEqual(self.engine.models.autonomy(), "locked")

    def test_locked_blocks_every_agent_model_change_with_a_helpful_message(self):
        self.mode("locked")
        before = len(self.events())
        for name, args in (("models.configure", {"model": "large", "provider": "codex"}),
                           ("models.switch", {"phase": "coding"}),
                           ("models.switch", {"phase": "coding", "model": "large", "provider": "codex"}),
                           ("models.set_default", {"provider": "claude", "model": "opus"})):
            with self.assertRaises(DomainError) as caught:
                self.tool(name, args)
            self.assertEqual(caught.exception.status, 403)
            self.assertIn("locked", str(caught.exception))
        self.assertEqual(self.store.settings()["default_model"], "small")
        self.assertEqual(len(self.events()), before)

    def test_locked_subagents_inherit_the_team_default_and_owner_policy(self):
        self.mode("locked")
        child = self.tool("agents.create", {"name": "Coder", "role": "Coding", "provider": "claude", "model": "opus",
                                             "model_policy": {"default": {"provider": "claude", "model": "sonnet"}}})
        saved = self.agent(child["id"])
        self.assertEqual(saved["binding_origin"], "default")
        self.assertEqual(self.engine.models.effective(saved)["model"], "small")
        self.assertEqual(saved["model_policy"], {})
        self.store.patch("agents", "a-lead", {"model_policy": {"subagents": {"phases": PHASES, "initial_phase": "planning"}}})
        policy_child = self.agent(self.tool("agents.create", {"name": "Worker", "role": "Coding"})["id"])
        self.assertEqual(policy_child["model_policy"]["phases"]["coding"]["model"], "sonnet")
        self.assertEqual(policy_child["binding_origin"], "default")

    def test_phases_allows_switch_and_configuring_only_agents_you_created(self):
        self.mode("phases")
        child = self.tool("agents.create", {"name": "Coder", "role": "Coding"})["id"]
        for target in ("a-lead", "a-code"):
            with self.assertRaises(DomainError) as caught:
                self.tool("models.configure", {"agent_id": target, "model": "large", "provider": "codex"})
            self.assertEqual(caught.exception.status, 403)
            self.assertIn("models.switch", str(caught.exception))
        with self.assertRaises(DomainError):
            self.tool("models.configure", {"model_policy": {"phases": PHASES}})          # Atlas itself
        run = self.engine.enqueue(child, "Work", project_id=self.run["project_id"])
        with self.assertRaises(DomainError):
            self.tool("models.configure", {"model": "large", "provider": "codex"}, actor=child, run=run)  # the caller itself
        self.tool("models.configure", {"agent_id": child, "model": "opus", "provider": "claude", "model_policy": {"phases": PHASES}})
        self.assertEqual(self.agent(child)["binding_origin"], "agent")
        grandchild = self.tool("agents.create", {"name": "Helper", "role": "Coding"}, actor=child, run=run)["id"]
        self.tool("models.configure", {"agent_id": grandchild, "model": "large", "provider": "codex"}, actor=child, run=run)
        self.assertEqual(self.agent(grandchild)["model"], "large")
        # temporary per-run switching still works and still respects the phase policy
        switched = self.tool("models.switch", {"phase": "coding"}, actor=child, run=run)
        self.assertEqual(switched["next_turn"]["model"], "sonnet")
        with self.assertRaises(DomainError):
            self.tool("models.switch", {"phase": "coding", "model": "opus", "provider": "claude"}, actor=child, run=run)
        free = self.tool("models.switch", {"model": "large", "provider": "codex", "phase": "research"})
        self.assertEqual(free["next_turn"]["model"], "large")

    def test_full_allows_other_agents_but_never_atlas_base_model(self):
        self.mode("full")
        self.tool("models.configure", {"agent_id": "a-code", "model": "large", "provider": "codex"})
        self.assertEqual(self.agent("a-code")["model"], "large")
        with self.assertRaises(DomainError) as caught:
            self.tool("models.configure", {"model": "opus", "provider": "claude"})
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(self.agent("a-lead")["binding_origin"], "default")
        self.tool("models.configure", {"model_policy": {"phases": PHASES}})              # phase policy on Atlas is still allowed
        self.assertEqual(self.agent("a-lead")["model_policy"]["phases"]["coding"]["model"], "sonnet")

    def test_owner_pin_blocks_agents_survives_default_changes_and_inherit_releases_it(self):
        self.mode("full")
        self.service.action("agents", "a-code", "model", {"provider": "claude", "model": "sonnet"})
        self.assertEqual(self.agent("a-code")["binding_origin"], "owner")
        with self.assertRaises(DomainError) as caught:
            self.tool("models.configure", {"agent_id": "a-code", "model": "large", "provider": "codex"})
        self.assertEqual(caught.exception.status, 403)
        self.engine.models.set_default({"provider": "claude", "model": "opus"})
        self.assertEqual(self.agent("a-code")["binding_origin"], "owner")
        self.assertEqual(self.engine.models.base(self.agent("a-code"))["model"], "sonnet")
        self.service.action("agents", "a-code", "model", {"inherit": True})
        self.assertEqual(self.agent("a-code")["binding_origin"], "default")
        self.assertEqual(self.engine.models.base(self.agent("a-code"))["model"], "opus")
        self.tool("models.configure", {"agent_id": "a-code", "model": "large", "provider": "codex"})
        self.assertEqual(self.agent("a-code")["binding_origin"], "agent")


class PickerScopeTests(AutonomyBase):
    def test_editing_atlas_model_in_the_agent_form_changes_the_team_default_not_a_pin(self):
        lead = self.agent("a-lead")
        self.service.update("agents", "a-lead", {"version": lead["version"], "name": lead["name"], "provider": "claude", "model": "opus"})
        lead = self.agent("a-lead")
        self.assertEqual((lead["binding_origin"], lead["model"]), ("default", "opus"))
        self.assertEqual(self.store.settings()["default_model"], "opus")
        self.assertEqual(self.events(kind="team_default")[-1]["actor_id"], "owner")
        self.engine.models.migrate()
        self.assertEqual(self.agent("a-lead")["model"], "opus")
        self.assertEqual(self.events(source="migration"), [])
        before = len(self.events(kind="team_default"))
        self.service.update("agents", "a-lead", {"version": self.agent("a-lead")["version"],
                                                 "name": lead["name"], "provider": "claude", "model": "opus"})
        self.assertEqual(len(self.events(kind="team_default")), before)

    def test_atlas_picker_defaults_to_this_chat_only(self):
        result = self.service.action("agents", "a-lead", "model", {"provider": "claude", "model": "sonnet"})
        self.assertEqual(result["scope"], "chat")
        settings = self.store.settings()
        self.assertEqual((settings["default_provider"], settings["default_model"]), ("codex", "small"))
        lead = self.agent("a-lead")
        self.assertEqual(lead["chat_model_override"]["model"], "sonnet")
        self.assertEqual(lead["binding_origin"], "default")
        self.assertEqual(self.store.get("runs", self.run["id"])["model_override"]["model"], "sonnet")
        self.assertEqual(self.agent("a-code")["binding_origin"], "default")
        self.assertEqual(self.events(kind="team_default", source="owner_chat"), [])
        self.assertEqual(len(self.events(kind="agent_model", agent_id="a-lead")), 1)
        self.service.action("agents", "a-lead", "model", {"inherit": True})
        self.assertIsNone(self.agent("a-lead")["chat_model_override"])

    def test_team_scope_changes_the_default_and_clears_the_chat_choice(self):
        self.service.action("agents", "a-lead", "model", {"provider": "claude", "model": "sonnet"})
        self.service.action("agents", "a-lead", "model", {"provider": "claude", "model": "opus", "scope": "team"})
        self.assertEqual(self.store.settings()["default_model"], "opus")
        self.assertIsNone(self.agent("a-lead")["chat_model_override"])
        self.assertEqual(self.engine.models.base(self.agent("a-code"))["model"], "opus")
        self.assertEqual(len(self.events(kind="team_default", source="owner_chat")), 1)
        with self.assertRaises(DomainError):
            self.service.action("agents", "a-lead", "model", {"provider": "claude", "model": "opus", "scope": "galaxy"})

    def test_model_selection_sent_with_a_message_honours_scope(self):
        self.engine.chat({"agent_id": "a-lead", "message": "Hello", "model_selection": {"provider": "claude", "model": "sonnet"}})
        self.assertEqual(self.store.settings()["default_model"], "small")
        self.assertEqual(self.agent("a-lead")["chat_model_override"]["model"], "sonnet")
        self.engine.chat({"agent_id": "a-lead", "message": "Hello again",
                          "model_selection": {"provider": "claude", "model": "opus", "scope": "team"}})
        self.assertEqual(self.store.settings()["default_model"], "opus")

    def test_commands_stay_team_scope_and_say_so(self):
        run = self.engine.chat({"agent_id": "a-lead", "message": "/model claude/opus"})
        self.assertEqual(self.store.settings()["default_model"], "opus")
        self.assertEqual(len(self.events(kind="team_default", source="owner_command")), 1)
        reply = [m for m in self.store.all("messages") if m.get("run_id") == run["id"] and m["role"] == "assistant"][-1]
        self.assertIn("cả đội", reply["content"])
        self.assertEqual(self.notes(), [])                                                # owner changes post no activity note
        self.engine.chat({"agent_id": "a-lead", "message": "Dùng Sonnet làm model mặc định cho đội"})
        self.assertEqual(self.store.settings()["default_model"], "sonnet")
        self.engine.chat({"agent_id": "a-code", "message": "/model codex/large"})
        self.assertEqual(self.agent("a-code")["binding_origin"], "owner")
        self.assertEqual(self.store.settings()["default_model"], "sonnet")


class ModelEventTests(AutonomyBase):
    def test_team_default_changes_are_recorded_and_noops_are_not(self):
        self.service.settings({"default_provider": "claude", "default_model": "opus"})
        events = self.events(kind="team_default")
        self.assertEqual(len(events), 2)                                                  # setUp's own change + this one
        last = events[-1]
        self.assertEqual((last["previous"]["model"], last["binding"]["model"], last["source"]), ("small", "opus", "owner_settings"))
        self.assertIsNone(last["actor_id"])
        self.assertEqual(self.store.settings()["default_model"], "opus")
        self.assertEqual(self.agent("a-code")["model"], "opus")
        self.service.action("agents", "a-code", "model", {"provider": "codex", "model": "large"})
        self.service.settings({"default_provider": "claude", "default_model": "opus"})
        self.engine.models.set_default({"provider": "claude", "model": "opus"})
        self.assertEqual(len(self.events(kind="team_default")), 2)
        self.assertEqual(self.agent("a-code")["binding_origin"], "owner")
        self.assertEqual(self.notes(), [])

    def test_agent_tool_change_records_actor_and_posts_one_activity_note(self):
        request = self.engine.chat({"agent_id": "a-lead", "message": "Dùng Opus làm model mặc định cho đội"})
        self.assertEqual(self.events(kind="team_default")[-1]["source"], "owner_command")
        self.assertEqual(self.notes(), [])
        # an agent applying a fresh owner request is recorded as agent_tool and announced in Atlas's DM
        self.engine.models.set_default({"provider": "codex", "model": "small"})
        self.store.settings({"model_autonomy": "phases"})
        again = self.engine.chat({"agent_id": "a-lead", "message": "Chọn Sonnet làm mặc định cho đội, không ghi tệp"})
        self.engine.models.set_default({"provider": "codex", "model": "small"})
        before = len(self.events(kind="team_default"))
        self.tool("models.set_default", {"provider": "claude", "model": "sonnet"}, run=again)
        events = self.events(kind="team_default")
        self.assertEqual(len(events), before + 1)
        self.assertEqual((events[-1]["source"], events[-1]["actor_id"]), ("agent_tool", "a-lead"))
        notes = self.notes()
        self.assertEqual(len(notes), 1)
        self.assertIn("codex/small → claude/sonnet", notes[0]["content"])
        self.assertEqual(request["status"], "done")

    def test_configure_and_owner_pick_record_agent_model_events(self):
        self.mode("full")
        self.tool("models.configure", {"agent_id": "a-code", "model": "large", "provider": "codex", "reason": "Needs depth"})
        event = self.events(kind="agent_model", agent_id="a-code")[-1]
        self.assertEqual((event["source"], event["actor_id"], event["binding"]["model"], event["reason"]),
                         ("agent_tool", "a-lead", "large", "Needs depth"))
        self.assertEqual(event["previous"]["model"], "small")
        self.service.action("agents", "a-content", "model", {"provider": "claude", "model": "sonnet"})
        event = self.events(kind="agent_model", agent_id="a-content")[-1]
        self.assertEqual((event["source"], event["actor_id"]), ("owner_chat", None))

    def test_migration_resets_atlas_to_follow_the_default_once(self):
        self.mode("full")
        self.store.patch("agents", "a-lead", {"provider": "claude", "model": "opus", "binding_origin": "agent",
                                              "model_policy": {"phases": PHASES, "initial_phase": "planning"}})
        self.service.action("agents", "a-code", "model", {"provider": "claude", "model": "sonnet"})
        revisions = len(self.store.all("agent_revisions"))
        self.engine.models.migrate()
        lead = self.agent("a-lead")
        self.assertEqual((lead["binding_origin"], lead["model"], lead["model_policy"]), ("default", "small", {}))
        self.assertEqual(len(self.store.all("agent_revisions")), revisions + 1)
        snapshot = self.store.all("agent_revisions")[-1]["snapshot"]
        self.assertEqual(snapshot["model"], "opus")
        migrated = self.events(source="migration")
        self.assertEqual(len(migrated), 1)
        self.assertEqual((migrated[0]["agent_id"], migrated[0]["previous"]["model"]), ("a-lead", "opus"))
        self.engine.models.migrate()
        self.assertEqual(len(self.events(source="migration")), 1)
        self.assertEqual(self.agent("a-code")["binding_origin"], "owner")                  # others are left alone

    def test_history_lists_events_newest_first_and_diverged_agents(self):
        self.service.action("agents", "a-code", "model", {"provider": "claude", "model": "sonnet"})
        self.mode("full")
        self.tool("models.configure", {"agent_id": "a-content", "model": "large", "provider": "codex"})
        self.engine.models.set_default({"provider": "claude", "model": "opus"})
        history = self.engine.models.history(2)
        self.assertEqual(len(history["events"]), 2)
        self.assertEqual(history["events"][0]["binding"]["model"], "opus")
        self.assertEqual(history["default"], {"provider": "claude", "model": "opus", "reasoning_effort": None})
        self.assertEqual(history["autonomy"], "full")
        diverged = {d["agent_id"]: d for d in history["diverged"]}
        self.assertEqual(set(diverged), {"a-code", "a-content"})
        self.assertEqual((diverged["a-code"]["binding_origin"], diverged["a-code"]["set_by"]), ("owner", "owner"))
        self.assertEqual((diverged["a-content"]["binding_origin"], diverged["a-content"]["set_by"]), ("agent", "a-lead"))
        self.assertEqual(diverged["a-content"]["binding"]["model"], "large")


class HistoryRouteTests(AutonomyBase):
    def test_get_history_route(self):
        self.service.action("agents", "a-code", "model", {"provider": "claude", "model": "sonnet"})
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service, "session", 0))
        port = server.server_port
        server.RequestHandlerClass = make_handler(self.service, "session", port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection("127.0.0.1", port)
        try:
            headers = {"Cookie": "af_session=session", "Origin": "http://127.0.0.1:%d" % port}
            conn.request("GET", "/api/models/history?limit=1", None, headers)
            res = conn.getresponse()
            body = json.loads(res.read())
            self.assertEqual(res.status, 200)
            self.assertEqual(set(body), {"events", "default", "autonomy", "diverged"})
            self.assertEqual(len(body["events"]), 1)
            self.assertEqual(body["autonomy"], "phases")
            self.assertEqual([d["agent_id"] for d in body["diverged"]], ["a-code"])
        finally:
            conn.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class QuotaEventTests(QuotaBase):
    def switch_all(self, autonomy=None):
        if autonomy:
            self.store.settings({"model_autonomy": autonomy})
        first = self.work("a-code")
        self.hit(first, text="limit · resets 10:30am (Asia/Ho_Chi_Minh)")
        self.pump()
        decision = self.decisions()[0]
        self.engine.team.answer(decision["id"], {"answer": "Switch all", "quota_action": "switch_all"})

    def notes(self):
        return [m for m in self.store.all("messages") if m.get("audience") == "activity" and "Team default model" in m["content"]]

    def test_switch_and_restore_are_logged_and_announced(self):
        self.switch_all()
        switched = [e for e in self.store.all("model_events") if e["source"] == "quota_switch"]
        self.assertEqual(len(switched), 1)
        self.assertEqual((switched[0]["previous"]["model"], switched[0]["binding"]["model"]), ("small", "sonnet"))
        self.assertIn("restores when Codex resets", switched[0]["reason"])
        self.assertEqual(len(self.notes()), 1)
        self.assertIn("codex/small → claude/sonnet", self.notes()[0]["content"])
        self.assertEqual((self.notes()[0]["kind"], self.notes()[0]["channel"]), ("progress", "dm-a-lead"))
        self.clock.now = self.episodes()[0]["retry_at"] + 1
        self.engine.wake_waiters(check_accounts=False)
        restored = [e for e in self.store.all("model_events") if e["source"] == "quota_restore"]
        self.assertEqual(len(restored), 1)
        self.assertEqual((restored[0]["previous"]["model"], restored[0]["binding"]["model"]), ("sonnet", "small"))
        self.assertEqual(len(self.notes()), 2)

    def test_quota_fallback_still_works_when_models_are_locked(self):
        self.switch_all("locked")
        self.assertEqual(self.store.settings()["default_model"], "sonnet")
        self.assertEqual(len([e for e in self.store.all("model_events") if e["source"] == "quota_switch"]), 1)


if __name__ == "__main__":
    unittest.main()
