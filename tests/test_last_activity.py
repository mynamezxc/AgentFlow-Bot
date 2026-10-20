import unittest
from unittest.mock import patch

import test_runtime as runtime
from agentflow.engine import Engine
from agentflow.store import DomainError


class LastActivityTests(unittest.TestCase):
    def setUp(self):
        runtime.RuntimeTests.setUp(self)
        self.run = self.engine.chat({"agent_id": "a-lead", "message": "Plan the launch"})

    tearDown = runtime.RuntimeTests.tearDown

    def stamp(self):
        return self.store.get("runs", self.run["id"]).get("last_activity_at")

    def test_touch_stamps_the_run_and_is_throttled(self):
        self.assertIsNone(self.stamp())
        self.assertTrue(self.engine.touch_run(self.run["id"]))
        first = self.stamp()
        self.assertTrue(first)
        version = self.store.get("runs", self.run["id"])["version"]
        for _ in range(5):
            self.assertFalse(self.engine.touch_run(self.run["id"]))
        self.assertEqual(self.store.get("runs", self.run["id"])["version"], version, "no extra writes inside the throttle window")
        self.engine.activity_marks[self.run["id"]] -= 11
        self.assertTrue(self.engine.touch_run(self.run["id"]))
        self.assertGreaterEqual(self.stamp(), first)
        self.assertEqual(self.store.get("runs", self.run["id"])["version"], version + 1)

    def test_runs_are_throttled_independently_and_unknown_runs_are_ignored(self):
        other = self.engine.chat({"agent_id": "a-lead", "message": "Another goal"})
        self.assertTrue(self.engine.touch_run(self.run["id"]))
        self.assertTrue(self.engine.touch_run(other["id"]))
        self.assertFalse(self.engine.touch_run("r-missing"))

    def test_agent_messages_count_as_activity(self):
        actor = self.store.get("agents", "a-lead")
        self.engine.team.emit(actor["id"], "Working on it", self.store.get("runs", self.run["id"]))
        self.assertTrue(self.stamp())

    def test_tool_execution_counts_as_activity(self):
        actor = self.store.get("agents", "a-lead")
        run = self.store.get("runs", self.run["id"])
        self.engine.execute_tool(actor, {"name": "tools.inspect", "arguments": {}}, run, 0)
        self.assertTrue(self.stamp())


class ModelCallTests(unittest.TestCase):
    def setUp(self):
        runtime.RuntimeTests.setUp(self)
        self.ready()
        self.task_run = self.engine.start_task("t-101")

    tearDown = runtime.RuntimeTests.tearDown
    ready = runtime.RuntimeTests.ready

    def run_doc(self):
        return self.store.get("runs", self.task_run["id"])

    def test_flag_is_set_during_the_call_and_cleared_after(self):
        seen = {}

        def infer(*args, **kwargs):
            seen["during"] = self.run_doc().get("model_call_started_at")
            return '{"reply":"Draft ready"}'

        with patch.object(self.providers, "infer", side_effect=infer):
            self.engine.execute(self.task_run["id"])
        self.assertTrue(seen["during"])
        self.assertIsNone(self.run_doc().get("model_call_started_at"))
        self.assertTrue(self.run_doc().get("last_activity_at"))

    def test_flag_is_cleared_when_the_call_raises(self):
        seen = {}

        def infer(*args, **kwargs):
            seen["during"] = self.run_doc().get("model_call_started_at")
            raise DomainError("Unavailable", 502)

        with patch.object(self.providers, "infer", side_effect=infer):
            self.engine.execute(self.task_run["id"])
        self.assertTrue(seen["during"])
        self.assertIsNone(self.run_doc().get("model_call_started_at"))

    def test_cancel_clears_the_flag(self):
        self.engine.begin_model_call(self.task_run["id"])
        self.assertTrue(self.run_doc().get("model_call_started_at"))
        self.engine.cancel(self.task_run["id"])
        self.assertIsNone(self.run_doc().get("model_call_started_at"))

    def test_restart_recovery_clears_the_flag(self):
        self.engine.begin_model_call(self.task_run["id"])
        self.store.patch("runs", self.task_run["id"], {"status": "running"})
        replacement = Engine(self.store, self.providers, self.vault, self.root / "company")
        try:
            self.assertIsNone(self.run_doc().get("model_call_started_at"))
        finally:
            replacement.close()


if __name__ == "__main__":
    unittest.main()
