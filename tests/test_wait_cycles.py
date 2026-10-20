"""Runs that wait on each other: review resume, the team.wait guard, the cycle breaker and the owner's Resume button."""
import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import test_runtime as runtime
from agentflow.store import DomainError
from server import make_handler

LAN, TUAN, HUY, CEO = "a-lead", "a-code", "a-seo", "a-review"


class Scenario(unittest.TestCase):
    """The owner's live deadlock: two tasks resubmitted after a revise, their first-round review runs still waiting,
    a peer chain CEO -> Lan -> task, Lan's task run and the CEO chat waiting on everything."""
    setUp0 = runtime.RuntimeTests.setUp
    tearDown = runtime.RuntimeTests.tearDown

    def setUp(self):
        self.setUp0()
        self.engine.waits.interval = 0
        self.engine.waits.grace = 0
        self.store.settings({"execution_enabled": True})
        self.build()

    def run_doc(self, agent, kind, status="waiting_team", **extra):
        root = getattr(self, "root", None)
        run = self.engine.enqueue(agent, "work of " + agent, kind, **({"root_id": root["id"]} if root else {}), **extra)
        return self.store.patch("runs", run["id"], {"status": status})

    def task(self, title, agent, **extra):
        return self.store.create("tasks", {"title": title, "description": "", "agent_id": agent, "reviewer_id": LAN, "coordinator_id": LAN,
            "status": "review", "auto_run": True, "auto_review": False, "dependencies": [], "priority": "medium", "revision": 1,
            "result": "Draft v2", "review_run_id": None, "last_review_run_id": None, "root_id": self.root["id"], "project_id": None, **extra})

    def build(self):
        self.root = self.engine.enqueue(CEO, "Ship the feature", "chat")
        self.store.patch("runs", self.root["id"], {"status": "waiting_team"})
        self.t1 = self.task("Backend API", TUAN)
        self.t2 = self.task("Frontend screen", HUY)
        # first-round review runs, both still waiting
        self.d1 = self.run_doc(LAN, "review", review_task_id=self.t1["id"], wait_for_tasks=[self.t1["id"]])
        self.p2 = self.run_doc(LAN, "peer", wait_for_tasks=[self.t1["id"]])           # Lan peer run waiting on the backend task
        self.p1 = self.run_doc(CEO, "peer", wait_for_runs=[self.p2["id"]])             # CEO peer run waiting on Lan's peer run
        self.d2 = self.run_doc(LAN, "review", review_task_id=self.t2["id"], wait_for_runs=[self.p1["id"]])
        self.r2 = self.run_doc(LAN, "task", wait_for_tasks=[self.t1["id"], self.t2["id"]])
        self.store.patch("runs", self.root["id"], {"wait_for_runs": [self.r2["id"]]})
        self.ids = [self.d1["id"], self.p2["id"], self.p1["id"], self.d2["id"], self.r2["id"], self.root["id"]]

    def status(self, run):
        return self.store.get("runs", run["id"])["status"]

    def review_as_lan(self, run, task, decision="approve"):
        actor = self.store.get("agents", LAN)
        self.engine.team.task_review(actor, {"task_id": task["id"], "decision": decision, "score": 5, "note": "Looks right."}, self.store.get("runs", run["id"]))
        self.store.patch("runs", run["id"], {"status": "done"})
        self.engine.wake_waiters(check_accounts=False)


class ReviewResumeTests(Scenario):
    def test_review_wakes_an_older_waiting_review_run_instead_of_returning_it_asleep(self):
        before = len(self.store.all("runs"))
        run = self.engine.review(self.t1["id"])
        self.assertEqual(run["id"], self.d1["id"])
        saved = self.store.get("runs", self.d1["id"])
        self.assertEqual((saved["status"], saved["wait_for_tasks"], saved["wait_for_runs"]), ("queued", [], []))
        self.assertEqual(saved["resume_reason"], "Revision 1 of Backend API was resubmitted. Review it now with tasks.review (approve or revise). "
                         "Do not call team.wait for this task: the runtime wakes you when it changes.")
        self.assertEqual(self.store.get("tasks", self.t1["id"])["review_run_id"], self.d1["id"])
        self.assertEqual(len(self.store.all("runs")), before)   # no duplicate review run
        again = self.engine.review(self.t1["id"])               # already queued: returned as is
        self.assertEqual((again["id"], again["resume_reason"]), (self.d1["id"], saved["resume_reason"]))

    def test_the_other_task_resumes_its_own_review_run_even_behind_a_peer_chain(self):
        self.engine.review(self.t2["id"])
        saved = self.store.get("runs", self.d2["id"])
        self.assertEqual((saved["status"], saved["wait_for_runs"]), ("queued", []))
        self.assertIn("Revision 1 of Frontend screen", saved["resume_reason"])
        self.assertEqual(self.store.get("tasks", self.t2["id"])["review_run_id"], self.d2["id"])

    def test_a_running_review_run_is_left_alone_but_linked_to_the_task(self):
        self.store.patch("runs", self.d1["id"], {"status": "running"})
        self.assertEqual(self.engine.review(self.t1["id"])["status"], "running")
        self.assertEqual(self.store.get("tasks", self.t1["id"])["review_run_id"], self.d1["id"])

    def test_dispatch_resumes_it_for_auto_review_tasks(self):
        self.store.patch("tasks", self.t1["id"], {"auto_review": True})
        self.engine.dispatch_tasks()
        self.assertEqual(self.status(self.d1), "queued")

    def test_submit_resumes_it(self):
        task = self.store.patch("tasks", self.t1["id"], {"status": "running", "review_run_id": None})
        self.engine.team.submit_task(self.store.get("agents", TUAN), {"task_id": task["id"], "result": "Resubmitted draft"})
        self.assertEqual(self.status(self.d1), "queued")
        self.assertEqual(self.store.get("tasks", self.t1["id"])["review_run_id"], self.d1["id"])

    def test_finish_of_the_worker_run_goes_through_review(self):
        self.store.patch("tasks", self.t1["id"], {"status": "running", "auto_review": True})
        worker = self.run_doc(TUAN, "task", status="running", task_id=self.t1["id"])
        self.store.patch("tasks", self.t1["id"], {"run_id": worker["id"]})
        self.engine.finish(self.store.get("runs", worker["id"]), self.store.get("agents", TUAN), "All done, files attached.")
        self.assertEqual(self.store.get("tasks", self.t1["id"])["status"], "review")
        self.assertEqual(self.status(self.d1), "queued")
        self.assertEqual(self.store.get("tasks", self.t1["id"])["review_run_id"], self.d1["id"])


class GuardTests(Scenario):
    def wait(self, run, **args):
        return self.engine.tool(self.store.get("agents", run["agent_id"]), {"name": "team.wait", "arguments": args}, self.store.get("runs", run["id"]))

    def test_reviewer_cannot_wait_on_the_task_of_her_review_run(self):
        for status in ("review", "ready"):   # also after her own "revise"
            self.store.patch("tasks", self.t1["id"], {"status": status})
            with self.assertRaises(DomainError) as caught:
                self.wait(self.d1, task_ids=[self.t1["id"]])
            self.assertEqual(caught.exception.status, 409)
            self.assertIn("You are the reviewer: the runtime wakes you when the draft is resubmitted; finish this turn.", str(caught.exception))

    def test_the_run_that_reviewed_a_task_cannot_wait_on_it(self):
        other = self.run_doc(LAN, "chat", status="running")
        self.store.patch("tasks", self.t2["id"], {"last_review_run_id": other["id"]})
        with self.assertRaises(DomainError) as caught:
            self.wait(other, task_ids=[self.t2["id"]])
        self.assertIn("You are the reviewer", str(caught.exception))
        self.store.patch("tasks", self.t2["id"], {"last_review_run_id": None, "review_run_id": other["id"]})
        with self.assertRaises(DomainError):
            self.wait(other, task_ids=[self.t2["id"]])

    def test_a_coordinator_run_may_wait_for_delegated_tasks_it_also_reviews(self):
        self.assertEqual(self.wait(self.r2, task_ids=[self.t1["id"], self.t2["id"]]), {"waiting": True})   # tasks in review, Lan is the reviewer
        self.assertEqual(self.store.get("runs", self.r2["id"])["wait_for_tasks"], [self.t1["id"], self.t2["id"]])

    def test_normal_waits_still_work(self):
        self.store.patch("tasks", self.t1["id"], {"status": "running"})
        self.assertEqual(self.wait(self.r2, task_ids=[self.t1["id"]]), {"waiting": True})   # reviewer waiting for work that is still being done
        self.assertEqual(self.wait(self.p1, task_ids=[self.t2["id"]]), {"waiting": True})   # a non-reviewer may wait for a task in review


class BreakerTests(Scenario):
    def test_the_owners_deadlock_is_found_and_unwound(self):
        resumed = self.engine.waits.sweep()
        self.assertEqual(resumed, [self.d1["id"]])   # one run per cycle: the reviewer's review run, closest to the work
        saved = self.store.get("runs", self.d1["id"])
        self.assertEqual((saved["status"], saved["wait_for_tasks"]), ("queued", []))
        self.assertIn("Revision 1 of Backend API was resubmitted", saved["resume_reason"])
        self.assertIn("Wait cycle detected", saved["resume_reason"])
        self.assertEqual(self.store.get("tasks", self.t1["id"])["review_run_id"], self.d1["id"])
        for other in (self.p2, self.p1, self.d2, self.r2):
            self.assertEqual(self.status(other), "waiting_team", other["id"])
        # the chain unwinds as the reviewer works: review t1 -> Lan peer -> CEO peer -> review t2 -> Lan task -> CEO chat
        self.review_as_lan(self.d1, self.t1)
        self.assertEqual(self.store.get("tasks", self.t1["id"])["status"], "done")
        self.assertEqual(self.status(self.p2), "queued")
        self.store.patch("runs", self.p2["id"], {"status": "done"})
        self.engine.wake_waiters(check_accounts=False)
        self.assertEqual(self.status(self.p1), "queued")
        self.store.patch("runs", self.p1["id"], {"status": "done"})
        self.engine.wake_waiters(check_accounts=False)
        self.assertEqual(self.status(self.d2), "queued")
        self.assertEqual(self.engine.waits.sweep(), [])   # nothing is stuck any more
        self.store.patch("tasks", self.t2["id"], {"review_run_id": self.d2["id"]})
        self.review_as_lan(self.d2, self.t2)
        self.assertEqual(self.status(self.r2), "queued")
        self.store.patch("runs", self.r2["id"], {"status": "done"})
        self.engine.wake_waiters(check_accounts=False)
        self.assertEqual(self.status(self.root), "queued")

    def test_it_records_one_event_and_one_activity_note_and_is_idempotent(self):
        self.engine.waits.sweep()
        self.assertEqual(self.engine.waits.sweep(), [])
        self.engine.wake_waiters(check_accounts=False)
        events = [e for e in self.store.all("events") if e["title"] == "Wait cycle resolved"]
        self.assertEqual(len(events), 1)
        self.assertIn(self.d1["id"], events[0]["detail"])
        notes = [m for m in self.store.all("messages") if m.get("audience") == "activity" and "waiting on each other" in m["content"]]
        self.assertEqual(len(notes), 1)
        self.assertIn("review of 'Backend API'", notes[0]["content"])
        self.assertEqual(notes[0]["channel"], self.store.get("runs", self.root["id"]).get("owner_channel") or self.store.get("runs", self.root["id"])["channel"])

    def test_a_worker_and_reviewer_waiting_on_each_other_are_broken_at_the_deepest_waiter(self):
        task = self.store.patch("tasks", self.t2["id"], {"status": "running", "revision": 0})
        worker = self.run_doc(HUY, "task", task_id=task["id"])
        peer = self.run_doc(LAN, "peer", wait_for_tasks=[task["id"]], parent_run_id=worker["id"])
        self.store.patch("runs", worker["id"], {"wait_for_runs": [peer["id"]]})
        self.store.patch("tasks", task["id"], {"run_id": worker["id"]})
        self.store.patch("runs", self.d2["id"], {"status": "done"})   # leave just this cycle plus the first one
        resumed = self.engine.waits.sweep()
        self.assertIn(peer["id"], resumed)
        self.assertNotIn(worker["id"], resumed)

    def test_recent_activity_is_not_touched_until_the_grace_period_passes(self):
        self.engine.waits.grace = 3600
        self.assertEqual(self.engine.waits.sweep(), [])
        self.assertEqual(self.status(self.d1), "waiting_team")
        self.assertEqual(self.engine.waits.sweep(manual=True), [self.d1["id"]])   # the owner's button ignores it

    def test_progressing_work_is_never_called_stuck(self):
        self.store.patch("runs", self.d1["id"], {"status": "queued"})   # the reviewer is working again
        self.assertEqual(self.engine.waits.sweep(), [])
        self.assertEqual(self.status(self.p2), "waiting_team")

    def test_a_task_in_review_with_no_reviewer_run_is_not_a_dead_wait_when_auto_review_will_start_one(self):
        self.store.patch("runs", self.d1["id"], {"status": "cancelled"})
        self.store.patch("tasks", self.t1["id"], {"auto_review": True})
        self.store.patch("runs", self.d2["id"], {"status": "done"})
        self.store.patch("tasks", self.t2["id"], {"status": "done"})
        self.assertEqual(self.engine.waits.sweep(), [])

    def test_a_dead_wait_on_a_review_nobody_can_do_is_resumed(self):
        self.store.patch("runs", self.d1["id"], {"status": "cancelled"})
        self.store.patch("runs", self.d2["id"], {"status": "cancelled"})
        resumed = self.engine.waits.sweep()
        self.assertTrue(resumed)
        self.assertEqual(self.status(self.store.get("runs", resumed[0])), "queued")

    def test_start_runs_the_sweep_once_before_the_loop(self):
        with patch.object(self.engine.waits, "sweep") as sweep, patch.object(self.engine.telegram, "start"):
            self.engine.start()
        sweep.assert_called_once_with(force=True)

    def test_every_wake_sweep_runs_the_breaker(self):
        self.engine.wake_waiters(check_accounts=False)
        self.assertEqual(self.status(self.d1), "queued")


class LimitTests(Scenario):
    def open_decisions(self):
        return [d for d in self.store.all("decisions") if d.get("action_handler") == "wait_cycle" and d["status"] == "open"]

    def stick_again(self):
        self.store.patch("runs", self.d1["id"], {"status": "waiting_team", "wait_for_tasks": [self.t1["id"]]})

    def test_after_three_repeats_the_owner_is_asked_once(self):
        for round_number in range(3):
            self.assertEqual(self.engine.waits.sweep(), [self.d1["id"]], round_number)
            self.stick_again()
        self.assertEqual(self.engine.waits.sweep(), [])
        self.assertEqual(self.engine.waits.sweep(), [])
        decisions = self.open_decisions()
        self.assertEqual(len(decisions), 1)
        question = decisions[0]
        self.assertEqual([o["label"] for o in question["actions"]], ["Resume", "Cancel branch"])
        self.assertEqual(question["agent_id"], LAN)
        self.assertIn("keep waiting on each other", question["question"])
        self.assertEqual(self.status(self.d1), "waiting_team")
        self.assertEqual(len([e for e in self.store.all("events") if e["title"] == "Wait cycle resolved"]), 3)

    def test_cancel_branch_stops_the_stuck_runs(self):
        for _ in range(3):
            self.engine.waits.sweep()
            self.stick_again()
        self.engine.waits.sweep()
        question = self.open_decisions()[0]
        self.engine.team.answer(question["id"], {"answer": "Cancel branch"})
        self.assertEqual(self.status(self.d1), "cancelled")
        self.assertEqual(self.engine.waits.sweep(), [])

    def test_resume_answer_tries_once_more_and_resets_the_counter(self):
        for _ in range(3):
            self.engine.waits.sweep()
            self.stick_again()
        self.engine.waits.sweep()
        question = self.open_decisions()[0]
        self.engine.team.answer(question["id"], {"answer": "Resume"})
        self.assertEqual(self.status(self.d1), "queued")
        self.assertEqual(self.engine.waits.breaks(self.root["id"]), 1)

    def test_manual_resume_ignores_the_limit(self):
        self.store.patch("runs", self.root["id"], {"wait_cycle_breaks": 3})
        self.assertEqual(self.engine.waits.sweep(), [])
        self.assertEqual(self.engine.unstick(self.root["id"])["resumed"], [self.d1["id"]])


class UnstickTests(Scenario):
    def request(self, path, body=None):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service, "session", 0))
        port = server.server_port
        server.RequestHandlerClass = make_handler(self.service, "session", port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection("127.0.0.1", port)
        try:
            conn.request("POST", path, json.dumps(body or {}), {"Cookie": "af_session=session", "Content-Type": "application/json", "Origin": "http://127.0.0.1:%d" % port})
            response = conn.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            conn.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_unstick_route_runs_the_breaker_for_that_root_now(self):
        self.engine.waits.grace = 3600   # the automatic sweep would wait; the button does not
        status, body = self.request("/api/runs/%s/unstick" % self.root["id"])
        self.assertEqual(status, 200)
        self.assertEqual(body["resumed"], [self.d1["id"]])
        self.assertEqual(body["message"], "Resumed 1 run.")
        self.assertEqual(self.status(self.d1), "queued")
        status, body = self.request("/api/runs/%s/unstick" % self.root["id"])   # a second press finds nothing left to break
        self.assertEqual((status, body["resumed"]), (200, []))

    def test_unstick_reports_when_nothing_is_stuck(self):
        self.engine.waits.sweep()
        self.engine.waits.sweep(manual=True)
        self.store.patch("runs", self.d1["id"], {"status": "running"})
        self.store.patch("runs", self.d2["id"], {"status": "running"})
        result = self.engine.unstick(self.root["id"])
        self.assertEqual(result["resumed"], [])
        self.assertIn("No wait cycle", result["message"])

    def test_unstick_unknown_root_is_a_404(self):
        status, body = self.request("/api/runs/run-missing/unstick")
        self.assertEqual(status, 404)

    def test_when_every_run_of_the_root_waits_the_button_resumes_the_deepest_one(self):
        self.store.patch("runs", self.d1["id"], {"status": "cancelled"})
        self.store.patch("runs", self.d2["id"], {"status": "cancelled"})
        self.store.patch("tasks", self.t1["id"], {"status": "ready", "auto_run": False})   # waiting on the owner, which the graph does not call a deadlock
        self.store.patch("tasks", self.t2["id"], {"status": "ready", "auto_run": False})
        for run in (self.p2, self.p1):
            self.store.patch("runs", run["id"], {"status": "cancelled"})
        result = self.engine.unstick(self.root["id"])
        self.assertEqual(len(result["resumed"]), 1)
        self.assertEqual(result["resumed"][0], self.r2["id"])


if __name__ == "__main__":
    unittest.main()
