import json
import threading
from pathlib import Path
from unittest.mock import patch

import unittest
import test_runtime as runtime
from agentflow.store import DomainError
from agentflow.runtime import Engine


class AutonomyTests(unittest.TestCase):
    setUp = runtime.RuntimeTests.setUp
    tearDown = runtime.RuntimeTests.tearDown
    ready = runtime.RuntimeTests.ready
    # Keep shared fixture and exercise new orchestration semantics.
    def request(self, name, args, run=None, actor="a-lead"):
        return self.engine.tool(self.store.get("agents", actor), {"name": name, "arguments": args}, run or self.run)

    def newrun(self):
        self.ready()
        self.run = self.engine.chat({"message": "Launch project", "agent_id": "a-lead", "channel": "dm-a-lead"})
        return self.run

    def test_agent_changes_and_skill_versions_need_no_owner_gate(self):
        self.newrun()
        child = self.request("agents.create", {"name": "Designer", "role": "Creative"})
        self.assertEqual(child["model"], "observed-test-model")
        self.request("agents.update", {"agent_id": child["id"], "name": "Project Manager", "role": "Product logic"})
        self.assertEqual(self.store.get("agents", child["id"])["role"], "Product logic")
        for args in ({"model": "hijack"}, {"provider": "gateway"}, {"credentials": "secret"}):
            with self.assertRaises(DomainError):
                self.request("agents.update", {"agent_id": child["id"], **args})
        with self.assertRaises(DomainError):
            self.request("agents.create", {"name": "No", "role": "No", "model": "hijack"})
        skill = self.request("skills.create", {"name": "Product checks", "content": "# Checks\nRead sources, verify outputs, report evidence."})
        self.assertIn(skill["id"], self.store.get("agents", "a-lead")["skills"])
        self.request("skills.update", {"skill_id": skill["id"], "content": "# Checks v2\nCheck assumptions and run tests."})
        self.assertEqual(len(self.store.all("skill_versions")), 1)
        self.assertTrue((self.engine.workspace / "skills" / skill["id"] / "SKILL.md").is_file())
        self.request("skills.install", {"skill_id": skill["id"], "agent_id": child["id"]})
        self.assertIn(skill["id"], self.store.get("agents", child["id"])["skills"])

    def test_long_work_continues_across_slices_and_sends_multiple_messages(self):
        run=self.newrun()
        outputs=[json.dumps({"reply": f"Step {i}", "messages":[f"Update {i}"], "state":"continue"}) for i in range(8)]
        outputs.append('{"reply":"Done","state":"complete"}')
        with patch.object(self.providers,"infer",side_effect=outputs) as inference:
            self.engine.execute(run["id"])
            self.assertEqual(self.store.get("runs",run["id"])["status"],"queued")
            self.engine.execute(run["id"])
        self.assertEqual(inference.call_count,9)
        self.assertEqual(self.store.get("runs",run["id"])["status"],"done")
        self.assertEqual(len([m for m in self.store.all("messages") if m.get("role")=="assistant" and m.get("run_id")==run["id"]]),17)

    def test_decision_resumes_exact_checkpoint_without_duplicate_side_effects(self):
        run=self.newrun()
        output=json.dumps({"reply":"Choose direction","tool_calls":[
            {"name":"skills.create","arguments":{"name":"Launch","content":"# Launch\nVerify target."}},
            {"name":"decisions.ask","arguments":{"question":"Which audience?","options":["Shops","Agencies"]}}]})
        with patch.object(self.providers,"infer",return_value=output): self.engine.execute(run["id"])
        self.assertEqual(self.store.get("runs",run["id"])["status"],"waiting_user")
        q=self.store.all("decisions")[-1]
        self.engine.team.answer(q["id"],{"answer":"Shops"})
        self.assertEqual(self.store.get("runs",run["id"])["status"],"queued")
        with patch.object(self.providers,"infer",return_value='{"reply":"Using Shops","state":"complete"}'): self.engine.execute(run["id"])
        self.assertEqual(len([s for s in self.store.all("skills") if s["name"]=="Launch"]),1)
        self.assertEqual(self.store.get("runs",run["id"])["status"],"done")
        with self.assertRaises(DomainError): self.engine.team.answer(q["id"],{"answer":"Agencies"})

    def test_many_peer_hops_and_request_reply_false(self):
        self.newrun()
        origin=self.run
        for i,target in enumerate(["a-seo","a-content","a-review","a-code"]):
            reply=self.request("team.message",{"agent_id":target,"content":f"Discuss step {i}","wait":True},origin,origin["agent_id"])
            origin=self.store.get("runs",reply["run_id"])
        self.assertEqual(len([r for r in self.store.all("runs") if r["kind"]=="peer"]),4)
        count=len(self.store.all("runs"))
        self.request("team.message",{"agent_id":"a-tester","content":"FYI","request_reply":False},origin,origin["agent_id"])
        self.assertEqual(len(self.store.all("runs")),count)
        for _ in range(3): result=self.request("team.message",{"agent_id":"a-seo","content":"Exact repeat"},self.run)
        self.assertFalse(result["delivered"])

    def test_independent_review_revision_restarts_automatically(self):
        run=self.newrun()
        taskid=self.request("tasks.delegate",{"title":"Write launch","agent_id":"a-content","reviewer_id":"a-review"})["task_id"]
        self.ready("a-content");self.ready("a-review")
        self.engine.dispatch_tasks()
        task=self.store.get("tasks",taskid)
        with patch.object(self.providers,"infer",return_value='{"reply":"Draft 1"}'):self.engine.execute(task["run_id"])
        self.engine.dispatch_tasks()
        task=self.store.get("tasks",taskid)
        reviewrun=self.store.get("runs",task["review_run_id"])
        with self.assertRaises(DomainError):
            self.request("tasks.review",{"task_id":taskid,"decision":"approve","score":5,"note":"Self approve"},reviewrun,"a-content")
        self.request("tasks.review",{"task_id":taskid,"decision":"revise","score":2,"note":"Need source"},reviewrun,"a-review")
        self.store.patch("runs",reviewrun["id"],{"status":"done"})
        self.engine.dispatch_tasks()
        task=self.store.get("tasks",taskid)
        self.assertEqual(task["status"],"running")
        self.assertIn("Need source",self.store.get("runs",task["run_id"])["prompt"])
        with patch.object(self.providers,"infer",return_value='{"reply":"Draft 2 with source"}'):self.engine.execute(task["run_id"])
        self.engine.dispatch_tasks()
        task=self.store.get("tasks",taskid)
        self.assertIsNotNone(task["review_run_id"])
        self.assertNotEqual(task["review_run_id"],reviewrun["id"])

    def test_queued_input_while_working_and_pause_resume(self):
        run=self.newrun()
        self.store.patch("agents","a-lead",{"status":"working"})
        second=self.engine.chat({"message":"Additional constraints","agent_id":"a-lead"})
        self.assertEqual(second["status"],"queued")
        self.store.patch("agents","a-lead",{"status":"paused"})
        self.engine.execute(run["id"])
        self.assertEqual(self.store.get("runs",run["id"])["status"],"queued")
        self.store.patch("agents","a-lead",{"status":"idle"})
        with patch.object(self.providers,"infer",return_value='{"reply":"Done"}'): self.engine.execute(run["id"])
        self.assertEqual(self.store.get("runs",run["id"])["status"],"done")

    def test_folder_grant_is_reused_and_readonly_enforced(self):
        run=self.newrun()
        external=self.root/"product";external.mkdir();(external/"README.md").write_text("Product brief")
        self.engine.team.grant_folder(run["project_id"],{"path":str(external),"access":"read"})
        args={"project_id":run["project_id"],"path":"README.md"}
        self.assertEqual(self.request("project.read",args)["content"],"Product brief")
        self.assertEqual(self.request("project.read",args)["content"],"Product brief")
        blocked=self.request("project.write",{**args,"content":"Changed"})
        self.assertTrue(blocked["waiting_permission"])
        self.assertEqual((external/"README.md").read_text(),"Product brief")
        self.request("workspace.write",{"path":"launch/report.md","content":"Findings"})
        artifact=self.store.all("artifacts")[-1]
        self.assertEqual(artifact["project_id"],run["project_id"])
        self.assertTrue((self.engine.workspace/"launch/report.md").is_file())

    def test_checkpoint_tool_replay_is_idempotent_and_cascade_cancels(self):
        run=self.newrun()
        call={"name":"agents.create","arguments":{"name":"Researcher","role":"Research"}}
        first=self.engine.execute_tool(self.store.get("agents","a-lead"),call,run,0)
        second=self.engine.execute_tool(self.store.get("agents","a-lead"),call,run,0)
        self.assertEqual(first["id"],second["id"])
        peer=self.request("team.message",{"agent_id":"a-seo","content":"Research this"})
        self.engine.cancel(run["id"],cascade=True)
        self.assertEqual(self.store.get("runs",peer["run_id"])["status"],"cancelled")

    def test_customer_cannot_create_agents_skills_or_read_private_files(self):
        run=self.newrun();run["kind"]="support"
        for name,args in (("agents.create",{"name":"Bad","role":"Admin"}),("skills.create",{"content":"# Bad"}),("workspace.read",{}),("memory.write",{"content":"Inject"})):
            with self.assertRaises(DomainError): self.request(name,args,run,"a-support")

    def test_execution_unavailable_is_honest_and_no_host_fallback(self):
        self.newrun()
        with patch("agentflow.files.shutil.which",return_value=None),patch("agentflow.files.subprocess.Popen") as process:
            with self.assertRaises(DomainError):self.request("workspace.execute",{"command":["python","test.py"]})
        process.assert_not_called()

    def test_natural_review_handoff_breaks_circular_wait(self):
        self.newrun();self.ready("a-content")
        taskid=self.request("tasks.delegate",{"title":"Brief","agent_id":"a-content","reviewer_id":"a-review"})["task_id"]
        worker=self.engine.start_task(taskid)
        self.request("workspace.write",{"path":"qa/brief.md","content":"Draft with assumptions"},worker,"a-content")
        peer=self.request("team.message",{"agent_id":"a-review","content":"Review this brief","wait":True},worker,"a-content")
        self.store.patch("runs",worker["id"],{"status":"waiting_team","last_reply":"Draft at qa/brief.md"})
        self.store.patch("runs",peer["run_id"],{"status":"waiting_team","wait_for_tasks":[taskid]})
        self.engine.wake_waiters()
        self.assertEqual(self.store.get("tasks",taskid)["status"],"review")
        reviewer=self.store.get("runs",peer["run_id"])
        self.assertEqual(reviewer["status"],"queued")
        self.assertEqual(reviewer["review_task_id"],taskid)
        self.request("tasks.review",{"task_id":taskid,"decision":"approve","score":5,"note":"Checked real file"},reviewer,"a-review")
        self.engine.finish(worker,self.store.get("agents","a-content"),"Done")
        self.assertEqual(self.store.get("tasks",taskid)["status"],"done")

    def test_submit_task_invokes_reviewer_and_prevents_self_wait(self):
        self.newrun();self.ready("a-content");self.ready("a-review")
        taskid=self.request("tasks.delegate",{"title":"Draft","agent_id":"a-content","reviewer_id":"a-review"})["task_id"]
        worker=self.engine.start_task(taskid)
        writer=self.store.get("agents","a-content")
        self.store.patch("agents",writer["id"],{"tools":writer["tools"]+["tasks.submit"]})
        result=self.request("tasks.submit",{"task_id":taskid,"result":"Draft in qa.md"},worker,"a-content")
        self.assertEqual(result["status"],"review")
        self.assertIsNotNone(self.store.get("tasks",taskid)["review_run_id"])

    def test_parallel_actors_create_consistent_records(self):
        self.newrun()
        errors=[]
        def worker(i):
            try:self.request("memory.write",{"title":str(i),"content":str(i)})
            except Exception as e:errors.append(e)
        threads=[threading.Thread(target=worker,args=(i,)) for i in range(20)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(errors,[])
        self.assertEqual(len([m for m in self.store.all("memories") if m["title"].isdigit()]),20)

    def test_new_project_in_same_action_batch_uses_live_metadata(self):
        run=self.newrun();actor=self.store.get("agents","a-lead")
        first=self.engine.execute_tool(actor,{"name":"projects.create","arguments":{"title":"New project"}},run,0)
        second=self.engine.execute_tool(actor,{"name":"workspace.write","arguments":{"path":"new/report.md","content":"Actual result"}},run,1)
        self.assertEqual(second["project_id"],first["id"])
        self.engine.cancel(run["id"])
        result=self.engine.execute_tool(actor,{"name":"agents.create","arguments":{"name":"Cancelled","role":"No"}},run,2)
        self.assertIn("error",result)
        self.assertFalse(any(a["name"]=="Cancelled" for a in self.store.all("agents")))

    def test_local_skill_discovery_is_installable_and_deduplicated(self):
        self.newrun()
        root=self.root/"local-skills";folder=root/"guide";folder.mkdir(parents=True)
        (folder/"SKILL.md").write_text("---\nname: Launch Guide\ndescription: Launch checks\n---\n# Guide\nVerify sources.",encoding="utf-8")
        result=self.engine.skills.scan([(root,"Test registry")])
        self.assertEqual(result["imported"],1)
        skillid=result["skills"][0]["id"]
        self.assertFalse(self.store.get("skills",skillid)["installed"])
        self.assertEqual(self.engine.skills.scan([(root,"Test registry")])["imported"],0)
        self.request("skills.install",{"skill_id":skillid})
        self.assertTrue(self.store.get("skills",skillid)["installed"])

    def test_telegram_decision_outbox_and_scoped_answer_resume(self):
        self.ready()
        self.vault.values["TELEGRAM_WEBHOOK_SECRET"]="test-secret"
        self.store.settings({"telegram_chats":"123,456"})
        headers={"X-Telegram-Bot-Api-Secret-Token":"test-secret"}
        payload={"update_id":501,"message":{"chat":{"id":123},"text":"Launch project"}}
        result=self.engine.webhook("telegram",json.dumps(payload).encode(),headers)
        run=self.store.get("runs",result["queued"][0])
        q=self.engine.team.ask(self.store.get("agents","a-lead"),{"question":"Audience?","options":["Shops","Teams"]},run)
        self.store.patch("runs",run["id"],{"status":"waiting_user"})
        outbox=self.store.all("channel_outbox")
        self.assertEqual(len(outbox),1)
        self.assertIn("/answer "+q["decision_id"],outbox[0]["content"])
        wrong={"update_id":502,"message":{"chat":{"id":456},"text":"/answer "+q["decision_id"]+" Shops"}}
        with self.assertRaises(DomainError):self.engine.webhook("telegram",json.dumps(wrong).encode(),headers)
        correct={"update_id":503,"message":{"chat":{"id":123},"text":"/answer "+q["decision_id"]+" Shops"}}
        self.engine.webhook("telegram",json.dumps(correct).encode(),headers)
        self.assertEqual(self.store.get("runs",run["id"])["status"],"queued")
        self.assertEqual(len(self.store.all("runs")),1)
        with patch("agentflow.channels.http_json",return_value={"ok":True}):
            self.engine.deliver_outbox(outbox[0])
        self.assertEqual(self.store.get("channel_outbox",outbox[0]["id"])["status"],"sent")

    def test_natural_chat_answer_requires_new_real_owner_message(self):
        run=self.newrun()
        q=self.request("decisions.ask",{"question":"Audience?","options":["Shops","Teams"]})
        self.store.patch("runs",run["id"],{"status":"waiting_user"})
        with self.assertRaises(DomainError):self.request("decisions.record_answer",{"decision_id":q["decision_id"]},run)
        followup=self.engine.chat({"message":"Choose Shops for the launch","agent_id":"a-lead","channel":run["channel"],"project_id":run["project_id"]})
        actor=self.store.get("agents","a-lead")
        self.store.patch("agents",actor["id"],{"tools":actor["tools"]+["decisions.record_answer"]})
        self.request("decisions.record_answer",{"decision_id":q["decision_id"],"answer":"Teams"},followup)
        self.assertEqual(self.store.get("decisions",q["decision_id"])["answer"],"Choose Shops for the launch")
        self.assertEqual(self.store.get("runs",run["id"])["status"],"queued")
