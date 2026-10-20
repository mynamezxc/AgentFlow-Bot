import http.client
import json
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import test_runtime as runtime
from agentflow.store import DomainError
from server import make_handler


class OwnerViewBase(unittest.TestCase):
    def setUp(self):
        runtime.RuntimeTests.setUp(self)
        self.providers.models = [{"id": "small", "provider": "codex", "name": "Small"}]
        self.engine.models.set_default({"provider": "codex", "model": "small"})
        self.run = self.engine.chat({"agent_id": "a-lead", "message": "Build a research team"})

    tearDown = runtime.RuntimeTests.tearDown

    def tool(self, name, args, actor="a-lead", run=None):
        return self.engine.tool(self.store.get("agents", actor), {"name": name, "arguments": args}, run or self.run)

    def cards(self):
        return [m for m in self.store.all("messages") if m.get("kind") == "agent_event"]

    def project(self):
        return self.store.get("projects", self.run["project_id"])


class AgentEventTests(OwnerViewBase):
    def test_creating_an_agent_posts_one_compact_owner_visible_card(self):
        child = self.tool("agents.create", {"name": "Mira", "role": "Researcher"})
        card = self.cards()[0]
        self.assertEqual((card["audience"], card["channel"], card["agent_id"], card["event"]), ("owner", self.run["channel"], "a-lead", "created"))
        self.assertIn("Atlas created **Mira**", card["content"])
        self.assertIn("Researcher", card["content"])
        binding = self.engine.models.base(self.store.get("agents", child["id"]))
        self.assertIn(binding["provider"] + "/" + binding["model"], card["content"])
        self.assertEqual([i["id"] for i in card["items"]], [child["id"]])
        self.assertEqual(card["subject_ids"], [child["id"]])
        self.assertNotEqual(card["kind"], "progress")

    def test_duplicate_name_creation_posts_no_second_card(self):
        self.tool("agents.create", {"name": "Mira", "role": "Researcher"})
        self.tool("agents.create", {"name": "mira", "role": "Researcher"})
        self.assertEqual(len(self.cards()), 1)

    def test_creations_from_one_run_within_60_seconds_share_one_card(self):
        first = self.tool("agents.create", {"name": "Mira", "role": "Researcher"})
        second = self.tool("agents.create", {"name": "Leo", "role": "Coder"})
        cards = self.cards()
        self.assertEqual(len(cards), 1)
        self.assertEqual([i["id"] for i in cards[0]["items"]], [first["id"], second["id"]])
        self.assertIn("created 2 agents", cards[0]["content"])
        self.assertIn("**Mira**", cards[0]["content"])
        self.assertIn("**Leo**", cards[0]["content"])
        with patch.object(type(self.engine.team), "recent", return_value=False):          # the window has passed
            self.tool("agents.create", {"name": "Zed", "role": "Tester"})
        self.assertEqual(len(self.cards()), 2)
        other = self.engine.chat({"agent_id": "a-lead", "message": "Another request"})
        self.tool("agents.create", {"name": "Ann", "role": "Analyst"}, run=other)
        self.assertEqual(len(self.cards()), 3)

    def test_updating_an_agent_posts_a_card_naming_the_changes(self):
        child = self.tool("agents.create", {"name": "Mira", "role": "Researcher"})
        self.tool("agents.update", {"agent_id": child["id"], "instructions": "Cite every source."})
        card = self.cards()[-1]
        self.assertEqual((card["event"], card["audience"]), ("updated", "owner"))
        self.assertIn("Atlas updated **Mira**: instructions", card["content"])
        self.assertEqual(card["subject_ids"], [child["id"]])
        self.assertEqual(len(self.cards()), 2)

    def test_cards_are_not_replayed_to_agents_as_conversation_history(self):
        self.tool("agents.create", {"name": "Mira", "role": "Researcher"})
        from agentflow.prompting import PromptBuilder
        history = PromptBuilder(self.engine).history(self.store.get("agents", "a-lead"), self.run, 50)
        self.assertFalse([m for m in history if m.get("kind") == "agent_event"])


class GoalTests(OwnerViewBase):
    def test_existing_projects_get_their_title_and_brief_as_the_first_goal(self):
        legacy = self.store.create("projects", {"title": "Spring launch", "brief": "Plan the spring launch", "channel": "dm-a-lead",
                                                "status": "active", "folder_grants": []})
        finished = self.store.create("projects", {"title": "Done work", "brief": "Old", "channel": "dm-x", "status": "done", "folder_grants": []})
        self.engine.goals.migrate()
        goals = self.store.get("projects", legacy["id"])["goals"]
        self.assertEqual([(g["title"], g["text"], g["status"], g["created_by"]) for g in goals], [("Spring launch", "Plan the spring launch", "active", "owner")])
        self.assertTrue(goals[0]["id"] and goals[0]["created_at"])
        self.assertEqual(self.store.get("projects", finished["id"])["goals"][0]["status"], "done")
        self.engine.goals.migrate()
        self.assertEqual(self.store.get("projects", legacy["id"])["goals"], goals)
        self.assertEqual(self.project()["goals"][0]["text"], "Build a research team")      # new chats start with one goal

    def test_agents_add_dedupe_update_and_complete_but_never_remove(self):
        added = self.tool("project.goals", {"action": "add", "text": "Publish the weekly report", "title": "Weekly report"})
        self.assertEqual(added["goal"]["title"], "Weekly report")
        self.assertEqual(len(added["active_goals"]), 2)
        again = self.tool("project.goals", {"action": "add", "text": "  publish   the weekly REPORT "})
        self.assertEqual(again["goal"]["id"], added["goal"]["id"])
        self.assertIn("already active", again["note"])
        self.assertEqual(len(self.project()["goals"]), 2)
        self.tool("project.goals", {"action": "update", "goal_id": added["goal"]["id"], "title": "Weekly report v2"})
        self.assertEqual(self.project()["goals"][1]["title"], "Weekly report v2")
        done = self.tool("project.goals", {"action": "complete", "goal_id": added["goal"]["id"]})
        self.assertEqual([g["id"] for g in done["active_goals"]], [self.project()["goals"][0]["id"]])
        kept = self.project()["goals"][1]
        self.assertEqual((kept["status"], kept["completed_by"]), ("done", "a-lead"))
        self.assertEqual(len(self.project()["goals"]), 2)                                   # completing never deletes
        for bad in ({"action": "remove", "goal_id": kept["id"]}, {"action": "delete"}, {"action": "complete", "goal_id": "missing"},
                    {"action": "add", "text": " "}):
            with self.assertRaises(DomainError):
                self.tool("project.goals", bad)
        self.assertEqual(len(self.project()["goals"]), 2)

    def test_only_the_owner_removes_a_goal_through_the_route_action(self):
        added = self.tool("project.goals", {"action": "add", "text": "Second goal"})["goal"]
        result = self.service.action("projects", self.run["project_id"], "goal-remove", {"goal_id": added["id"]})
        self.assertEqual(result["removed"]["id"], added["id"])
        self.assertEqual([g["text"] for g in self.project()["goals"]], ["Build a research team"])
        with self.assertRaises(DomainError) as caught:
            self.service.action("projects", self.run["project_id"], "goal-remove", {"goal_id": added["id"]})
        self.assertEqual(caught.exception.status, 404)

    def test_goal_remove_http_route(self):
        added = self.tool("project.goals", {"action": "add", "text": "Route goal"})["goal"]
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service, "session", 0))
        port = server.server_port
        server.RequestHandlerClass = make_handler(self.service, "session", port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection("127.0.0.1", port)
        try:
            headers = {"Cookie": "af_session=session", "Content-Type": "application/json", "Origin": "http://127.0.0.1:%d" % port}
            conn.request("POST", "/api/projects/%s/goals/%s/remove" % (self.run["project_id"], added["id"]), "{}", headers)
            response = conn.getresponse()
            body = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(body["removed"]["id"], added["id"])
        finally:
            conn.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(len(self.project()["goals"]), 1)

    def test_goal_command_adds_without_replacing_and_skips_repeats(self):
        project = self.project()
        first = self.engine.slash.goal({"channel": self.run["channel"], "goal": "Grow newsletter signups"})["project"]
        self.assertEqual(first["id"], project["id"])
        self.assertEqual(first["title"], project["title"])
        self.assertEqual([g["text"] for g in first["goals"]], ["Build a research team", "Grow newsletter signups"])
        again = self.engine.slash.goal({"channel": self.run["channel"], "goal": "grow newsletter  SIGNUPS"})["project"]
        self.assertEqual(len(again["goals"]), 2)
        self.engine.slash.goal({"channel": self.run["channel"], "goal": "Build a research team"})
        self.assertEqual(len(self.project()["goals"]), 2)

    def test_agent_context_lists_only_active_goals(self):
        added = self.tool("project.goals", {"action": "add", "text": "Ship the demo"})["goal"]
        self.tool("project.goals", {"action": "complete", "goal_id": self.project()["goals"][0]["id"]})
        context = self.engine.context(self.store.get("agents", "a-lead"), run=self.run)
        self.assertEqual([g["id"] for g in context["project"]["active_goals"]], [added["id"]])

    def test_goal_tool_is_available_to_agents_without_enabling_it(self):
        from agentflow.contract import TOOLS
        self.assertIn("project.goals", TOOLS)
        actor = self.store.get("agents", "a-code")
        worker = self.engine.enqueue("a-code", "Work", project_id=self.run["project_id"])
        self.assertTrue(self.engine.tool(actor, {"name": "project.goals", "arguments": {"action": "add", "text": "Code goal"}}, worker)["goal"]["id"])


class DeliverableTests(OwnerViewBase):
    def artifact(self, path):
        return next(a for a in self.store.all("artifacts") if a["path"] == path)

    def test_envelope_files_flag_the_artifacts_as_deliverables(self):
        actor = self.store.get("agents", "a-lead")
        self.tool("workspace.write", {"path": "reports/final.md", "content": "# Final"})
        self.tool("workspace.write", {"path": "scratch/notes.md", "content": "working"})
        self.assertFalse(self.artifact("reports/final.md").get("deliverable"))
        result = self.engine.envelope_files(self.run, actor, {"files": ["reports/final.md"]}, [])
        self.assertEqual(len(result["ids"]), 1)
        self.assertTrue(self.artifact("reports/final.md")["deliverable"])
        self.assertFalse(self.artifact("scratch/notes.md").get("deliverable"))

    def test_envelope_files_wait_for_a_tool_free_turn_and_flag_then(self):
        actor = self.store.get("agents", "a-lead")
        self.tool("workspace.write", {"path": "reports/later.md", "content": "x"})
        pending = self.engine.envelope_files(self.run, actor, {"files": ["reports/later.md"]}, [{"name": "x"}])
        self.assertEqual((pending["ids"], pending["pending"]), ([], ["reports/later.md"]))
        self.assertFalse(self.artifact("reports/later.md").get("deliverable"))
        self.engine.envelope_files({**self.run, "pending_files": pending["pending"]}, actor, {}, [])
        self.assertTrue(self.artifact("reports/later.md")["deliverable"])

    def test_write_and_register_accept_an_explicit_deliverable_flag(self):
        self.tool("workspace.write", {"path": "demo/page.md", "content": "demo", "deliverable": True})
        self.assertTrue(self.artifact("demo/page.md")["deliverable"])
        self.tool("workspace.write", {"path": "demo/other.md", "content": "demo", "deliverable": "yes"})
        self.assertFalse(self.artifact("demo/other.md").get("deliverable"))
        self.tool("workspace.write", {"path": "demo/third.md", "content": "demo"})
        self.tool("outputs.register", {"path": "demo/third.md", "title": "Third", "deliverable": True})
        self.assertTrue(self.artifact("demo/third.md")["deliverable"])
        self.assertEqual(self.artifact("demo/third.md")["title"], "Third")

    def test_files_the_agent_did_not_author_are_not_flagged(self):
        actor = self.store.get("agents", "a-lead")
        outside = self.engine.workspace / "owner-notes.md"
        outside.write_text("mine", encoding="utf-8")
        past = time.time() - 3600
        import os
        os.utime(outside, (past, past))
        self.engine.envelope_files(self.run, actor, {"files": ["owner-notes.md"]}, [])
        self.assertFalse(self.artifact("owner-notes.md").get("deliverable"))


class UsageNoticeTests(OwnerViewBase):
    def test_observed_codex_usage_no_longer_carries_a_default_notice(self):
        state = self.providers.accounts.check("codex", force=True)
        payload = {"limits": {"rateLimitsByLimitId": {"codex": {"primary": {"usedPercent": 37, "windowDurationMins": 300, "resetsAt": time.time() + 1000}}}}}
        with patch.object(self.providers, "_codex_models", return_value=payload):
            self.providers.usage.refresh_codex(state, force=True)
        public = self.providers.usage.public("codex")
        self.assertEqual(public["windows"][0]["used_percent"], 37)
        self.assertFalse(public["notice"])
        self.assertNotIn("no inference", str(public))
        self.assertNotIn("Quota reported by", str(public))


if __name__ == "__main__":
    unittest.main()
