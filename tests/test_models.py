import io
import json
import os
import unittest
from unittest.mock import Mock, patch

import test_runtime as runtime
from agentflow.models import ModelUnavailable
from agentflow.accounts import ProviderLimit
from agentflow.native import claude_account, claude_catalog, discover_cli
from agentflow.runtime import Engine


class ModelRoutingTests(unittest.TestCase):
    def setUp(self):
        runtime.RuntimeTests.setUp(self)
        self.providers.models = [
            {"id": "small", "provider": "codex", "name": "Small"},
            {"id": "large", "provider": "codex", "name": "Large"},
            {"id": "default", "provider": "claude", "name": "Default", "resolved_model": "claude-opus-test"},
            {"id": "opus", "provider": "claude", "name": "Opus", "resolved_model": "claude-opus-test"},
            {"id": "sonnet", "provider": "claude", "name": "Sonnet", "resolved_model": "claude-sonnet-test"},
        ]
        self.store.settings({"model_autonomy": "full"})  # these tests exercise agent-driven model routing
        self.engine.models.set_default({"provider": "codex", "model": "small"})
        self.run = self.engine.chat({"agent_id": "a-lead", "message": "Build a team"})

    tearDown = runtime.RuntimeTests.tearDown

    def tool(self, name, args, actor="a-lead", run=None):
        return self.engine.tool(self.store.get("agents", actor), {"name": name, "arguments": args}, run or self.run)

    def phases(self):
        return {"analysis": {"provider": "claude", "model": "opus"},
                "planning": {"provider": "claude", "model": "opus"},
                "coding": {"provider": "claude", "model": "sonnet"}}

    def test_atlas_default_is_live_inheritance_and_private_choice_survives(self):
        child = self.tool("agents.create", {"name": "Coder", "role": "Coding"})
        self.service.action("agents", "a-content", "model", {"provider": "claude", "model": "sonnet"})
        self.service.action("agents", "a-lead", "model", {"provider": "claude", "model": "opus", "scope": "team"})
        for agent_id in ("a-lead", "a-code", child["id"]):
            selected = self.engine.models.effective(self.store.get("agents", agent_id))
            self.assertEqual((selected["provider"], selected["model"]), ("claude", "opus"))
        selected = self.engine.models.effective(self.store.get("agents", "a-content"))
        self.assertEqual(selected["model"], "sonnet")
        self.service.action("agents", "a-content", "model", {"inherit": True})
        self.assertEqual(self.engine.models.effective(self.store.get("agents", "a-content"))["model"], "opus")

    def test_private_chat_selection_does_not_change_team_default(self):
        run = self.engine.chat({"agent_id": "a-code", "message": "Review code",
            "model_selection": {"provider": "claude", "model": "sonnet"}})
        self.assertEqual(self.engine.models.effective(self.store.get("agents", "a-code"), run)["model"], "sonnet")
        self.assertEqual(self.store.settings()["default_model"], "small")

    def test_chat_commands_and_simple_default_request_work_without_inference(self):
        with patch.object(self.providers, "infer") as inference:
            run = self.engine.chat({"agent_id": "a-lead", "message": "Dùng Opus làm model mặc định cho đội"})
            self.assertEqual(run["status"], "done")
            self.assertEqual(self.store.settings()["default_model"], "opus")
            private = self.engine.chat({"agent_id": "a-code", "message": "/model claude/sonnet"})
            self.assertEqual(private["status"], "done")
            self.assertEqual(self.engine.models.base(self.store.get("agents", "a-code"))["model"], "sonnet")
            self.engine.chat({"agent_id": "a-code", "message": "/model inherit"})
            self.assertEqual(self.engine.models.base(self.store.get("agents", "a-code"))["model"], "opus")
            inventory = self.engine.chat({"agent_id": "a-lead", "message": "/models"})
            self.assertIn("claude/opus", inventory["result"])
        inference.assert_not_called()

    def test_phase_switch_changes_real_next_inference_and_preserves_context(self):
        self.tool("models.configure", {"model_policy": {"phases": self.phases(), "initial_phase": "planning"}})
        calls = []
        def infer(actor, prompt, run_id):
            calls.append((actor["provider"], actor["model"], prompt))
            if len(calls) == 1:
                return json.dumps({"reply": "Plan retained", "tool_calls": [
                    {"name": "models.switch", "arguments": {"phase": "coding", "reason": "Plan ready"}}]})
            self.assertIn("Plan retained", prompt)
            return '{"reply":"Coding complete"}'
        with patch.object(self.providers, "infer", side_effect=infer):
            self.engine.execute(self.run["id"])
        saved = self.store.get("runs", self.run["id"])
        self.assertEqual(saved["status"], "done")
        self.assertEqual([c[:2] for c in calls], [("claude", "opus"), ("claude", "sonnet")])
        self.assertEqual([h["phase"] for h in saved["model_history"]], ["planning", "coding"])

    def test_parent_policy_reaches_descendants_and_plan_workers(self):
        self.tool("models.configure", {"model_policy": {"subagents": {"phases": self.phases(), "initial_phase": "planning"}}})
        child = self.tool("agents.create", {"name": "Coding lead", "role": "Coding"})
        grandchild = self.tool("agents.create", {"name": "Worker", "role": "Implementation"}, actor=child["id"])
        for item in (child, grandchild):
            actor = self.store.get("agents", item["id"])
            self.assertEqual(self.engine.models.effective(actor)["model"], "opus")
            self.assertEqual(actor["model_policy"]["phases"]["coding"]["model"], "sonnet")
        plan = self.tool("plan.propose", {"title": "Implement", "subagents": [
            {"key": "worker", "name": "Planned Worker", "role": "Coding"}], "tasks": [
            {"title": "Write code", "agent_id": "worker", "phase": "coding", "auto_review": False}]})
        task = self.store.get("tasks", plan["task_ids"][0])
        run = self.engine.start_task(task["id"])
        self.assertEqual(self.engine.models.effective(self.store.get("agents", task["agent_id"]), run)["model"], "sonnet")

    def test_child_cannot_ignore_explicit_phase_model(self):
        self.tool("models.configure", {"model_policy": {"subagents": {"phases": self.phases()}}})
        child = self.tool("agents.create", {"name": "Strict worker", "role": "Coding"})
        run = self.engine.enqueue(child["id"], "Implement", project_id=self.run["project_id"])
        with self.assertRaises(runtime.DomainError):
            self.tool("models.switch", {"phase": "coding", "model": "opus", "provider": "claude"}, actor=child["id"], run=run)
        with self.assertRaises(runtime.DomainError):
            self.tool("models.configure", {"model_policy": {}}, actor=child["id"], run=run)

    def test_agent_can_choose_observed_initial_model_but_not_invent_one(self):
        child = self.tool("agents.create", {"name": "Planner", "role": "Planning", "provider": "claude", "model": "opus"})
        self.assertEqual(child["model"], "opus")
        self.assertEqual(self.engine.models.resolve("Soinet", "claude")["model"], "sonnet")
        self.assertEqual(self.engine.models.resolve("claude-opus-test", "claude")["model"], "claude-opus-test")
        with self.assertRaises(runtime.DomainError):
            self.tool("agents.create", {"name": "Unknown", "role": "Coding", "model": "made-up"})

    def test_model_inventory_is_visible_only_to_internal_agents(self):
        result = self.tool("models.list", {})
        self.assertEqual(len(result["models"]), 5)
        context = self.engine.context(self.store.get("agents", "a-code"), run=self.run)
        self.assertIn("available_models", context)
        self.assertNotIn("available_models", self.engine.context(self.store.get("agents", "a-support"), customer=True))
        with self.assertRaises(runtime.DomainError):
            self.tool("models.list", {}, run={**self.run, "kind": "support"})

    def test_only_real_atlas_owner_request_can_change_global_default(self):
        with self.assertRaises(runtime.DomainError):
            self.tool("models.set_default", {"provider": "claude", "model": "opus"})
        request = self.engine.chat({"agent_id": "a-lead", "message": "Dùng Opus làm model mặc định cho đội"})
        self.tool("models.set_default", {"provider": "claude", "model": "opus"}, run=request)
        self.assertEqual(self.store.settings()["default_model"], "opus")
        mixed = self.engine.chat({"agent_id": "a-lead", "message": "Chọn Sonnet làm mặc định cho đội, không ghi tệp"})
        self.tool("models.set_default", {"provider": "claude", "model": "sonnet"}, run=mixed)
        self.assertEqual(self.store.settings()["default_model"], "sonnet")
        peer = self.engine.enqueue("a-lead", "Default sonnet", "peer")
        with self.assertRaises(runtime.DomainError):
            self.tool("models.set_default", {"provider": "claude", "model": "sonnet"}, run=peer)

    def test_model_switch_and_phase_survive_restart(self):
        self.tool("models.switch", {"model": "large", "provider": "codex", "phase": "research"})
        replacement = Engine(self.store, self.providers, self.vault, self.engine.workspace)
        try:
            run = self.store.get("runs", self.run["id"])
            effective = replacement.models.effective(self.store.get("agents", "a-lead"), run)
            self.assertEqual((effective["model"], effective["phase"]), ("large", "research"))
        finally:
            replacement.close()

    def test_owner_private_choice_applies_to_next_turn_of_waiting_work(self):
        run = self.engine.chat({"agent_id": "a-code", "message": "Work"})
        self.tool("models.switch", {"provider": "codex", "model": "large"}, actor="a-code", run=run)
        self.service.action("agents", "a-code", "model", {"provider": "claude", "model": "sonnet"})
        selected = self.engine.models.effective(self.store.get("agents", "a-code"), self.store.get("runs", run["id"]))
        self.assertEqual((selected["provider"], selected["model"]), ("claude", "sonnet"))

    def test_private_chat_choice_can_start_session_then_switch_to_required_phase(self):
        self.tool("models.configure", {"agent_id": "a-code", "model_policy": {"phases": self.phases(), "initial_phase": "planning"}})
        self.service.action("agents", "a-code", "model", {"provider": "codex", "model": "large"})
        run = self.engine.chat({"agent_id": "a-code", "message": "Discuss next task"})
        selected = self.engine.models.effective(self.store.get("agents", "a-code"), run)
        self.assertEqual(selected["model"], "large")
        self.tool("models.switch", {"phase": "planning"}, actor="a-code", run=run)
        selected = self.engine.models.effective(self.store.get("agents", "a-code"), self.store.get("runs", run["id"]))
        self.assertEqual(selected["model"], "opus")

    def test_unavailable_model_waits_until_owner_selects_valid_default(self):
        with patch.object(self.providers, "infer", side_effect=ModelUnavailable("small")):
            self.engine.execute(self.run["id"])
        self.assertEqual(self.store.get("runs", self.run["id"])["status"], "blocked_model")
        self.providers.models = [m for m in self.providers.models if m["id"] != "small"]
        self.engine.wake_waiters(check_accounts=False)
        self.assertEqual(self.store.get("runs", self.run["id"])["status"], "blocked_model")
        self.engine.models.owner_select("a-lead", {"provider": "codex", "model": "large", "scope": "team"})
        self.assertEqual(self.store.get("runs", self.run["id"])["status"], "queued")

    def test_quota_wait_preserves_checkpoint_and_owner_model_change_resumes(self):
        error = ProviderLimit("claude", "You've hit your session limit · resets 10:30am (Asia/Ho_Chi_Minh)")
        self.assertGreater(error.retry_at, __import__('time').time())
        with patch.object(self.providers, "infer", side_effect=error):
            self.engine.execute(self.run["id"])
        saved = self.store.get("runs", self.run["id"])
        self.assertEqual(saved["status"], "waiting_quota")
        self.assertEqual(saved["steps"], 0)
        self.engine.wake_waiters(check_accounts=False)
        self.assertEqual(self.store.get("runs", self.run["id"])["status"], "waiting_quota")
        self.engine.models.owner_select("a-lead", {"provider": "codex", "model": "large", "scope": "team"})
        self.assertEqual(self.store.get("runs", self.run["id"])["status"], "queued")

    def test_discovery_finds_extension_binary_outside_path(self):
        binary = self.root / ".vscode/extensions/anthropic.claude-code-2.1.301-win32-x64/resources/native-binary/claude.exe"
        binary.parent.mkdir(parents=True)
        binary.write_text("fixture")
        with patch("agentflow.native.shutil.which", return_value=None), \
                patch.dict(os.environ, {"USERPROFILE": str(self.root), "CLAUDE_PATH": ""}):
            self.assertEqual(discover_cli("claude"), str(binary))

    def test_claude_catalog_uses_control_metadata_without_inference(self):
        response = {"type": "control_response", "response": {"subtype": "success", "request_id": "agentflow-catalog",
            "response": {"models": [{"value": "opus", "resolvedModel": "claude-opus-test"}]}}}
        process = Mock(stdin=io.StringIO(), stdout=io.StringIO(json.dumps(response) + "\n"))
        process.stdin.close = Mock()
        with patch("agentflow.native.subprocess.Popen", return_value=process) as launch:
            result = claude_catalog("claude.exe", {})
        self.assertEqual(result[0]["value"], "opus")
        request = json.loads(process.stdin.getvalue())
        self.assertEqual(request["request"]["subtype"], "initialize")
        self.assertIn("--safe-mode", launch.call_args.args[0])
        self.assertNotIn('"type": "user"', process.stdin.getvalue())

    def test_claude_identity_is_official_auth_metadata_without_secrets(self):
        result = Mock(stdout=json.dumps({"loggedIn": True, "authMethod": "claude.ai", "email": "current@example.test",
            "orgId": "organization-a", "accessToken": "never-copy-me"}))
        with patch("agentflow.native.subprocess.run", return_value=result):
            identity = claude_account("claude.exe", {})
        self.assertEqual(identity["email"], "current@example.test")
        self.assertEqual(identity["org_id"], "organization-a")
        self.assertNotIn("never-copy-me", json.dumps(identity))
