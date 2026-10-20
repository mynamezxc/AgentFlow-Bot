import copy
import hashlib
import hmac
import http.client
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from agentflow.engine import Engine, next_schedule
from agentflow.providers import Providers, Vault
from agentflow.service import Service
from agentflow.store import DomainError, Store
from agentflow.research import public_page
from server import LifetimeLock, make_handler
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
SEED = json.loads((ROOT / "dist" / "seed.json").read_text(encoding="utf-8"))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "data", copy.deepcopy(SEED))
        self.vault = Vault(self.root / "data")
        self.providers = Providers(self.store, self.vault, self.root / "company")
        self.providers.cli = {name: None for name in ("codex", "claude", "gemini")}
        self.account_patch = patch.object(self.providers, "read_account",
            return_value={"type": "test", "label": "Unit account"})
        self.account_patch.start()
        self.engine = Engine(self.store, self.providers, self.vault, self.root / "company")
        self.service = Service(self.store, self.providers, self.vault, self.engine)

    def tearDown(self):
        self.engine.close()
        self.account_patch.stop()
        self.store.close()
        self.temp.cleanup()

    def ready(self, agent_id="a-lead"):
        self.store.settings({"execution_enabled": True})
        return self.store.patch("agents", agent_id, {"provider": "gateway", "model": "observed-test-model"})

    def test_crud_conflict_archive_and_persistence(self):
        item = self.service.create("memories", {"title": "Test", "content": "New knowledge", "scope": "shared"})
        updated = self.service.update("memories", item["id"], {"title": "Edited", "version": item["version"]})
        self.assertEqual(updated["title"], "Edited")
        with self.assertRaises(DomainError) as ctx:
            self.service.update("memories", item["id"], {"title": "Stale", "version": 1})
        self.assertEqual(ctx.exception.status, 409)
        archived = self.service.update("memories", item["id"], {"archived": True, "version": updated["version"]})
        self.assertTrue(archived["archived"])
        restored = self.service.update("memories", item["id"], {"archived": False, "version": archived["version"]})
        second = Store(self.root / "data", SEED)
        self.assertEqual(second.get("memories", item["id"])["title"], "Edited")
        self.assertFalse(restored["archived"])
        second.close()

    def test_memory_isolation(self):
        other = self.engine.context(self.store.get("agents", "a-content"))
        owner = self.engine.context(self.store.get("agents", "a-seo"))
        self.assertEqual(other["your_memory"], [])
        self.assertEqual(owner["your_memory"][0]["id"], "k-3")
        support = self.engine.context(self.store.get("agents", "a-support"), customer=True)
        self.assertNotIn("your_memory", support)
        self.assertNotIn("team", support)
        self.assertNotIn("tasks", support)

    def test_path_restrictions_and_roundtrip(self):
        for name in ("../secret.md", ".env", "private/.key.md", str(self.root / "escape.md"), "file.exe"):
            with self.assertRaises(DomainError):
                self.engine.path(name)
        a = self.store.get("agents", "a-lead")
        self.engine.tool(a, {"name": "workspace.write", "arguments": {"path": "drafts/test.md", "content": "hello"}}, {"id": "test"})
        result = self.engine.tool(a, {"name": "workspace.read", "arguments": {"path": "drafts/test.md"}}, {"id": "test"})
        self.assertEqual(result, "hello")
        with self.assertRaises(DomainError):
            self.engine.tool(self.store.get("agents", "a-support"), {"name": "workspace.read", "arguments": {}}, {"kind":"support"})

    def test_execution_defaults_to_authorized_autonomy(self):
        self.assertTrue(self.store.settings()['execution_enabled'])
        run=self.engine.start_task('t-101')
        self.engine.execute(run['id'])
        self.assertEqual(self.store.get('runs',run['id'])['status'],'blocked_model')
        self.assertEqual(len(self.store.all('decisions')),1)
        self.ready()
        self.engine.wake_waiters()
        self.assertEqual(self.store.get('runs',run['id'])['status'],'queued')

    def test_success_requires_review_and_prevents_duplicate(self):
        self.ready()
        run = self.engine.start_task("t-101")
        with self.assertRaises(DomainError):
            self.engine.start_task("t-101")
        with patch.object(self.providers, "infer", return_value='{"reply":"Draft ready"}'):
            self.engine.execute(run["id"])
        self.assertEqual(self.store.get("tasks", "t-101")["status"], "review")
        result = self.service.action("tasks", "t-101", "approve", {"score": 5, "note": "Checked"})
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["score"], 5)

    def test_provider_failure_does_not_complete_task(self):
        self.ready()
        run = self.engine.start_task("t-101")
        with patch.object(self.providers, "infer", side_effect=DomainError("Unavailable", 502)):
            for _ in range(4):  # three backoff retries (30s/2m/5m), then a clear failure
                self.store.patch("runs",run["id"],{"status":"queued"})
                self.engine.execute(run["id"])
        self.assertEqual(self.store.get("tasks", "t-101")["status"], "failed")
        self.assertEqual(self.store.get("runs", run["id"])["status"], "failed")

    def test_task_dependency_and_distinct_reviewer(self):
        self.ready("a-seo")
        self.store.patch("tasks", "t-105", {"status": "ready"})
        with self.assertRaises(DomainError):
            self.engine.start_task("t-105")
        with self.assertRaises(DomainError):
            self.service.create("tasks", {"title": "Invalid", "agent_id": "a-lead", "reviewer_id": "a-lead"})

    def test_model_switch_cannot_change_active_run(self):
        original = self.store.patch("agents", "a-lead", {"status": "working"})
        with self.assertRaises(DomainError):
            self.service.update("agents", "a-lead", {"model": "new", "version": original["version"]})

    def test_daily_schedule_uses_vietnam_timezone(self):
        at = datetime(2026, 10, 18, 1, 0, tzinfo=timezone.utc)
        following = next_schedule({"mode": "daily", "time": "08:30"}, at)
        self.assertEqual(datetime.fromisoformat(following).hour, 1)
        self.assertEqual(datetime.fromisoformat(following).minute, 30)
        tomorrow = next_schedule({"mode": "daily", "time": "07:00"}, at)
        self.assertEqual(datetime.fromisoformat(tomorrow).day, 19)
        with self.assertRaises(DomainError):
            next_schedule({"mode": "interval", "interval_minutes": 1}, at)

    def test_scheduler_dedupes_and_idle_uses_no_model(self):
        self.ready("a-seo")
        at = datetime(2026, 10, 18, 2, 0, tzinfo=timezone.utc)
        self.store.patch("schedules", "s-1", {"enabled": True, "next_run": "2026-10-18T01:30:00Z"})
        with patch.object(self.providers, "infer") as inference:
            self.engine.tick(at)
            self.engine.tick(at)
            inference.assert_not_called()
        self.assertEqual(len(self.store.all("runs")), 1)

    def test_plan_materializes_immediately_with_owner_binding(self):
        self.ready()
        before=len(self.store.all('tasks'))
        plan=self.engine.propose_plan(self.store.get('agents','a-lead'),{
            'title':'Plan','subagents':[{'key':'researcher','name':'Scout','role':'Research'}],
            'tasks':[{'title':'Research','agent_id':'researcher','reviewer_id':'a-review'},
                {'title':'Write','agent_id':'a-content','reviewer_id':'a-seo','depends_on':[0]}]})
        self.assertEqual(len(self.store.all('tasks')),before+2)
        self.assertEqual(self.store.all('agents')[-1]['model'],'observed-test-model')
        self.assertEqual(plan['status'],'active')
        self.assertEqual(self.store.get('tasks',plan['task_ids'][1])['dependencies'],[plan['task_ids'][0]])
        with self.assertRaises(DomainError):self.engine.approve_plan(plan['id'])

    def test_plan_cycles_and_self_review_are_rejected(self):
        a = self.store.get("agents", "a-lead")
        for task in [{"title": "X", "agent_id": "a-lead", "reviewer_id": "a-lead"},
                     {"title": "X", "agent_id": "a-lead", "reviewer_id": "a-review", "depends_on": [0]}]:
            with self.assertRaises(DomainError):
                self.engine.propose_plan(a, {"tasks": [task]})

    def test_tool_output_loop_is_bounded(self):
        self.ready()
        run = self.engine.enqueue("a-lead", "Remember")
        output = '{"reply":"working","tool_calls":[{"name":"memory.write","arguments":{"title":"x","content":"y"}}]}'
        with patch.object(self.providers, "infer", return_value=output) as inference:
            self.engine.execute(run["id"])
        self.assertEqual(inference.call_count, 4)
        self.assertEqual(self.store.get("runs", run["id"])["status"], "waiting_user")
        self.assertEqual(len(self.store.all("decisions")),1)

    def test_cancelled_run_cannot_write_tools(self):
        self.ready()
        run = self.engine.enqueue("a-lead", "Remember")
        self.engine.cancel(run["id"])
        with patch.object(self.providers, "infer") as inference:
            self.engine.execute(run["id"])
        inference.assert_not_called()
        self.assertEqual(self.store.get("runs", run["id"])["status"], "cancelled")

    def test_webhook_secret_allowlist_and_deduplication(self):
        self.ready()
        self.vault.set({"TELEGRAM_WEBHOOK_SECRET": "test-secret"})
        self.store.settings({"telegram_chats": "123"})
        body = json.dumps({"update_id": 42, "message": {"chat": {"id": 123}, "text": "hello"}}).encode()
        with self.assertRaises(DomainError):
            self.engine.webhook("telegram", body, {})
        headers = {"X-Telegram-Bot-Api-Secret-Token": "test-secret"}
        self.assertEqual(len(self.engine.webhook("telegram", body, headers)["queued"]), 1)
        self.assertEqual(self.engine.webhook("telegram", body, headers)["queued"], [])
        wrong = json.dumps({"update_id": 43, "message": {"chat": {"id": 999}, "text": "hello"}}).encode()
        with self.assertRaises(DomainError):
            self.engine.webhook("telegram", wrong, headers)

    def test_messenger_signature_and_customer_scope(self):
        self.ready("a-support")
        self.vault.set({"FACEBOOK_APP_SECRET": "app-secret"})
        self.store.settings({"page_id": "123", "messenger_enabled": True})
        body = json.dumps({"entry": [{"id": "123", "messaging": [{"sender": {"id": "user1"},
                    "timestamp": int(time.time()*1000), "message": {"mid": "msg-1", "text": "Price?"}}]}]}).encode()
        with self.assertRaises(DomainError):
            self.engine.webhook("messenger", body, {})
        signature = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
        result = self.engine.webhook("messenger", body, {"X-Hub-Signature-256": signature})
        self.assertEqual(len(result["queued"]), 1)
        self.assertEqual(self.store.get("runs", result["queued"][0])["kind"], "support")

    def test_publish_requires_approval_and_unknown_never_retries(self):
        with self.assertRaises(DomainError):
            self.engine.publish("p-1")
        self.vault.set({"FACEBOOK_PAGE_TOKEN": "test-token"})
        self.store.settings({"page_id": "123"})
        self.service.action("posts", "p-1", "approve", {})
        with patch("agentflow.channels.http_json", side_effect=DomainError("Timeout", 502)) as request:
            result = self.engine.publish("p-1")
            self.assertEqual(result["status"], "unknown")
            with self.assertRaises(DomainError):
                self.engine.publish("p-1")
            self.assertEqual(request.call_count, 1)

    def test_editing_approved_post_resets_approval(self):
        self.service.action("posts", "p-1", "approve", {})
        post = self.store.get("posts", "p-1")
        edited = self.service.update("posts", "p-1", {"body": "Changed", "version": post["version"]})
        self.assertEqual(edited["status"], "draft")

    def test_skill_import_is_inert_and_hashes_content(self):
        item = self.service.import_skill({"name": "Safe guide", "content": "# Skill\nInstructions", "category": "custom"})
        self.assertEqual(len(item["sha256"]), 64)
        self.assertTrue(item["installed"])
        with self.assertRaises(DomainError):
            self.service.import_skill({"name": "Bad", "url": "http://127.0.0.1/private/SKILL.md"})

    def test_vault_roundtrip_and_redaction(self):
        result = self.vault.set({"GATEWAY_API_KEY": "never-print-this-secret"})
        if result["persistence"] == "session":
            self.assertFalse(self.vault.path.exists())
            self.assertIn("notice", result)
            reopened = self.vault
        else:
            reopened = Vault(self.root / "data")
        self.assertEqual(reopened.get("GATEWAY_API_KEY"), "never-print-this-secret")
        self.assertNotIn("never-print-this-secret", reopened.redact("error never-print-this-secret"))
        self.assertNotIn("GATEWAY_API_KEY", json.dumps(self.store.snapshot()))

    def test_http_owner_session_origin_and_state(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service, "session-token", 0))
        port = server.server_port
        server.RequestHandlerClass = make_handler(self.service, "session-token", port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection("127.0.0.1", port)
        try:
            conn.request("GET", "/api/state")
            response = conn.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            conn.request("GET", "/")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("HttpOnly", response.getheader("Set-Cookie"))
            response.read()
            conn.request("GET", "/api/state", headers={"Cookie": "af_session=session-token"})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(len(json.loads(response.read())["agents"]), 8)
            body = json.dumps({"workspace_name": "Invalid origin"})
            conn.request("POST", "/api/settings", body, {"Content-Type": "application/json",
                         "Cookie": "af_session=session-token", "Origin": "https://evil.example"})
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            conn.request("POST", "/api/settings", json.dumps({"workspace_name": "Valid"}), {
                "Content-Type": "application/json", "Cookie": "af_session=session-token",
                "Origin": f"http://127.0.0.1:{port}"})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["workspace_name"], "Valid")
        finally:
            conn.close()
            server.shutdown()
            server.server_close()
            thread.join()

    def test_public_research_blocks_private_networks(self):
        for url in ("http://example.com", "https://localhost", "https://127.0.0.1", "https://[::1]", "https://example.com:abc"):
            with self.assertRaises(DomainError):
                public_page(url)

    def test_content_tool_only_creates_drafts(self):
        writer = self.store.get("agents", "a-content")
        post = self.engine.tool(writer, {"name": "content.draft", "arguments": {
            "title": "Blog draft", "body": "Needs review", "channel": "blog", "status": "published"}}, {"id": "test"})
        self.assertEqual(post["status"], "draft")
        self.assertEqual(post["agent_id"], writer["id"])

    def test_review_teaches_only_the_assigned_agent(self):
        self.service.action("tasks", "t-103", "approve", {"score": 4, "note": "Use stronger sources"})
        writer = self.engine.context(self.store.get("agents", "a-content"))
        reviewer = self.engine.context(self.store.get("agents", "a-review"))
        self.assertIn("Use stronger sources", writer["your_memory"][-1]["content"])
        self.assertFalse(any("Use stronger sources" in m["content"] for m in reviewer["your_memory"]))

    def test_task_edit_rejects_dependency_cycles(self):
        task = self.store.get("tasks", "t-102")
        with self.assertRaises(DomainError):
            self.service.update("tasks", "t-102", {"dependencies": ["t-105"], "version": task["version"]})

    def test_webhook_conversations_are_separate(self):
        self.ready()
        self.vault.values["TELEGRAM_WEBHOOK_SECRET"] = "test-secret"
        self.store.settings({"telegram_chats": "123,456"})
        for recipient in (123, 456):
            raw = json.dumps({"update_id": recipient, "message": {"chat": {"id": recipient}, "text": "hello"}}).encode()
            self.engine.webhook("telegram", raw, {"X-Telegram-Bot-Api-Secret-Token": "test-secret"})
        self.assertEqual({r["channel"] for r in self.store.all("runs")}, {"telegram:123", "telegram:456"})

    def test_restart_recovers_work_from_checkpoint(self):
        self.ready()
        run = self.engine.start_task("t-101")
        self.store.patch("posts", "p-1", {"status": "publishing"})
        replacement = Engine(self.store, self.providers, self.vault, self.root / "company")
        try:
            self.assertEqual(self.store.get("runs", run["id"])["status"], "queued")
            self.assertEqual(self.store.get("tasks", "t-101")["status"], "running")
            self.assertEqual(self.store.get("posts", "p-1")["status"], "unknown")
        finally:
            replacement.close()


if __name__ == "__main__":
    unittest.main()
