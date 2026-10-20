import json
import unittest
from pathlib import Path
from unittest.mock import patch

import test_runtime as runtime
from agentflow.runtime import Engine
from agentflow.store import DomainError


class AccessTests(unittest.TestCase):
    setUp = runtime.RuntimeTests.setUp
    tearDown = runtime.RuntimeTests.tearDown
    ready = runtime.RuntimeTests.ready

    def worker(self, actor="a-code"):
        self.ready(actor)
        return self.engine.chat({"message": "Review project source", "agent_id": actor, "channel": "dm-" + actor})

    def source(self):
        folder = self.root / "project source"
        folder.mkdir(exist_ok=True)
        (folder / "README.md").write_text("Actual project source", encoding="utf-8")
        (folder / "other.md").write_text("Sibling source", encoding="utf-8")
        return folder

    def call(self, name, args, run):
        return self.engine.tool(self.store.get("agents", run["agent_id"]),
                                {"name": name, "arguments": args}, run)

    def request(self, run, path, **kwargs):
        return self.call("files.request_access", {"path": str(path), "reason": "Review actual code", **kwargs}, run)

    def test_worker_requests_atlas_and_no_read_before_owner_decides(self):
        run, folder = self.worker(), self.source()
        with patch.object(self.engine.files, "read") as read:
            result = self.request(run, folder)
        read.assert_not_called()
        q = self.store.get("decisions", result["decision_id"])
        self.assertEqual(q["agent_id"], "a-lead")
        self.assertEqual(q["channel"], "dm-a-lead")
        self.assertEqual(q["requested_access"], "read")
        self.assertTrue(q["permission_request"])
        self.assertEqual(self.store.get("projects", run["project_id"])["folder_grants"], [])
        self.assertEqual(q["requesters"][0]["run_id"], run["id"])
        self.assertTrue(any(m.get("kind") == "peer" and m.get("target_id") == "a-lead"
                            for m in self.store.all("messages")))

    def test_shared_request_deduplicates_and_owner_answer_wakes_all_workers(self):
        first, folder = self.worker(), self.source()
        self.ready("a-seo")
        second = self.engine.enqueue("a-seo", "Read same project", "peer",
                                    project_id=first["project_id"], root_id=first["root_id"])
        q1, q2 = self.request(first, folder), self.request(second, folder)
        self.assertEqual(q1["decision_id"], q2["decision_id"])
        for run in (first, second): self.store.patch("runs", run["id"], {"status": "waiting_user"})
        self.engine.team.answer(q1["decision_id"], {"answer": "Cho phép đọc"})
        for run in (first, second):
            self.assertEqual(self.store.get("runs", run["id"])["status"], "queued")
            result = self.call("workspace.read", {"path": str(folder / "README.md")}, run)
            self.assertEqual(result, "Actual project source")
        self.assertEqual(len(self.store.all("decisions")), 1)
        self.assertEqual(self.request(second, folder)["status"], "granted")

    def test_pending_file_tool_retries_from_checkpoint_after_approval(self):
        run, folder = self.worker(), self.source()
        request = json.dumps({"reply": "Read source", "tool_calls": [
            {"name": "workspace.read", "arguments": {"path": str(folder / "README.md")}}]})
        with patch.object(self.providers, "infer", return_value=request):
            self.engine.execute(run["id"])
        saved = self.store.get("runs", run["id"])
        self.assertEqual(saved["status"], "waiting_user")
        self.assertEqual(saved["phase"], "tools")
        self.assertEqual(self.store.all("tool_events")[0]["status"], "waiting_permission")
        question = self.store.all("decisions")[0]
        self.engine.team.answer(question["id"], {"answer": "Cho phép đọc"})
        with patch.object(self.providers, "infer", return_value='{"reply":"Verified actual source"}'):
            self.engine.execute(run["id"])
        self.assertEqual(self.store.get("runs", run["id"])["status"], "done")
        events = self.store.all("tool_events")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "done")
        self.assertEqual(events[0]["result"], "Actual project source")

    def test_single_file_grant_does_not_grant_siblings(self):
        run, folder = self.worker(), self.source()
        result = self.request(run, folder / "README.md", target_type="file")
        self.engine.team.answer(result["decision_id"], {"answer": "Cho phép đọc"})
        project = self.store.get("projects", run["project_id"])
        grant = project["folder_grants"][0]
        self.assertEqual(grant["target_type"], "file")
        args = {"project_id": project["id"], "grant_id": grant["id"]}
        self.assertEqual(self.engine.files.read(args, project=True)["content"], "Actual project source")
        with self.assertRaises(DomainError):
            self.engine.files.read({**args, "path": "other.md"}, project=True)
        self.assertIsNone(self.engine.access.covered(project, folder / "other.md"))

    def test_denial_does_not_grant_even_with_prefilled_form_and_does_not_reask(self):
        run, folder = self.worker(), self.source()
        result = self.request(run, folder)
        self.store.patch("runs", run["id"], {"status": "waiting_user"})
        self.engine.team.answer(result["decision_id"], {"answer": "Không",
            "grant": {"path": str(folder), "access": "read"}})
        self.assertEqual(self.store.get("projects", run["project_id"])["folder_grants"], [])
        self.assertEqual(self.request(run, folder)["status"], "denied")
        self.assertEqual(len(self.store.all("decisions")), 1)
        self.assertEqual(self.store.get("runs", run["id"])["status"], "queued")

    def test_ambiguous_answer_keeps_request_open(self):
        run, folder = self.worker(), self.source()
        result = self.request(run, folder)
        with self.assertRaises(DomainError):
            self.engine.team.answer(result["decision_id"], {"answer": "Tôi đang xem xét"})
        self.assertEqual(self.store.get("decisions", result["decision_id"])["status"], "open")
        self.assertEqual(self.store.get("projects", run["project_id"])["folder_grants"], [])

    def test_read_grant_requires_separate_write_decision(self):
        run, folder = self.worker(), self.source()
        read = self.request(run, folder)
        self.engine.team.answer(read["decision_id"], {"answer": "Cho phép đọc"})
        write = self.call("project.write", {"project_id": run["project_id"], "path": "README.md", "content": "Changed"}, run)
        self.assertTrue(write["waiting_permission"])
        self.assertNotEqual(write["decision_id"], read["decision_id"])
        self.engine.team.answer(write["decision_id"], {"answer": "Đồng ý nhưng chỉ đọc"})
        denied = self.call("project.write", {"project_id": run["project_id"], "path": "README.md", "content": "Changed"}, run)
        self.assertTrue(denied["access_denied"])
        self.assertEqual((folder / "README.md").read_text(), "Actual project source")

    def test_tentative_or_negative_consent_never_grants_access(self):
        run, folder = self.worker(), self.source()
        result = self.request(run, folder)
        for text in ("Tôi chưa đồng ý", "Có nên cho phép không?", "Nếu cần thì cấp quyền", "Để tôi xem xét cấp quyền", "Tôi đang xem"):
            with self.subTest(text=text), self.assertRaises(DomainError):
                self.engine.team.answer(result["decision_id"], {"answer": text,
                    "grant": {"path": str(folder), "access": "read"}})
        self.assertEqual(self.store.get("decisions", result["decision_id"])["status"], "open")
        self.engine.team.answer(result["decision_id"], {"answer": "Không cần cấp quyền"})
        self.assertEqual(self.store.get("projects", run["project_id"])["folder_grants"], [])
        self.assertEqual(self.store.get("decisions", result["decision_id"])["permission_status"], "denied")

    def test_generic_folder_question_is_always_routed_by_atlas(self):
        run, folder = self.worker(), self.source()
        result = self.call("decisions.ask", {"kind": "folder_access", "question": "Read?",
            "requested_path": str(folder), "_routed_by_atlas": True}, run)
        question = self.store.get("decisions", result["decision_id"])
        self.assertEqual(question["agent_id"], "a-lead")
        self.assertEqual(question["channel"], "dm-a-lead")

    def test_refusal_is_converted_to_permission_request_instead_of_completion(self):
        run, folder = self.worker(), self.source()
        reply = "I cannot access the project outside workspace: " + str(folder)
        self.store.patch("runs", run["id"], {"prompt": 'Read "' + str(folder) + '"'})
        with patch.object(self.providers, "infer", return_value=json.dumps({"reply": reply})):
            self.engine.execute(run["id"])
        self.assertEqual(self.store.get("runs", run["id"])["status"], "waiting_user")
        self.assertEqual(self.store.all("decisions")[0]["agent_id"], "a-lead")

    def test_restart_recovers_existing_blocked_work_without_duplicate_questions(self):
        run, folder = self.worker(), self.source()
        self.store.patch("runs", run["id"], {"status": "done", "prompt": 'Read "' + str(folder) + '"',
            "result": "I cannot access files outside workspace."})
        replacement = Engine(self.store, self.providers, self.vault, self.engine.workspace)
        try:
            self.assertEqual(self.store.get("runs", run["id"])["status"], "waiting_user")
            replacement.access.recover_blocked()
            self.assertEqual(len(self.store.all("decisions")), 1)
        finally:
            replacement.close()

    def test_customer_and_protected_roots_cannot_request_access(self):
        run, folder = self.worker(), self.source()
        support = {**run, "kind": "support"}
        with self.assertRaises(DomainError): self.request(support, folder)
        with self.assertRaises(DomainError): self.request(run, self.store.directory)
        with self.assertRaises(DomainError): self.request(run, Path(folder.anchor))
        self.assertEqual(self.store.all("decisions"), [])

    def test_new_real_owner_chat_can_approve_request_from_different_project_view(self):
        run, folder = self.worker(), self.source()
        result = self.request(run, folder)
        answer = self.engine.chat({"message": "Đồng ý, cho phép đọc", "agent_id": "a-lead", "channel": "dm-a-lead"})
        context = self.engine.context(self.store.get("agents", "a-lead"), run=answer)
        self.assertEqual(context["pending_owner_decisions"][0]["id"], result["decision_id"])
        self.call("decisions.record_answer", {"decision_id": result["decision_id"]}, answer)
        self.assertEqual(self.store.get("decisions", result["decision_id"])["permission_status"], "granted")
