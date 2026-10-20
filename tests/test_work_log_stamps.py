"""Run work logs carry an `at` ISO stamp per note and tool call (the owner's collapsed work log orders rows with it);
entries saved before the stamp existed keep working, and /api/state still sends each run's bounded work_log."""
import unittest
from datetime import datetime

import test_runtime as runtime


class WorkLogStampTests(unittest.TestCase):
    setUp = runtime.RuntimeTests.setUp
    tearDown = runtime.RuntimeTests.tearDown

    def test_new_notes_and_calls_get_an_iso_stamp_and_old_entries_are_untouched(self):
        old = {"work_log": {"calls": [{"s": 1, "n": "workspace.read", "a": "path=a.md", "r": "ok", "e": False}],
                            "notes": [{"s": 1, "t": "Reading the brief"}]}}
        log = self.engine.work_log(old, calls=[{"s": 2, "n": "tasks.create", "a": "title=x", "r": "ok", "e": False}], note={"s": 2, "t": "Splitting the plan"})
        self.assertNotIn("at", log["calls"][0])   # legacy entry: the UI falls back to the run's own times
        self.assertNotIn("at", log["notes"][0])
        for entry in (log["calls"][1], log["notes"][1]):
            stamp = datetime.fromisoformat(entry["at"].replace("Z", "+00:00"))
            self.assertIsNotNone(stamp.tzinfo)
        self.assertEqual((log["calls"][1]["n"], log["notes"][1]["t"]), ("tasks.create", "Splitting the plan"))
        self.assertEqual(self.engine.work_log({"work_log": log})["notes"], log["notes"])   # no new note: nothing changes

    def test_an_existing_stamp_is_kept_and_the_log_stays_bounded(self):
        run = {"work_log": {"calls": [], "notes": []}}
        for step in range(100):
            run = {"work_log": self.engine.work_log(run, calls=[{"s": step, "n": "t", "a": "", "r": "", "e": False, "at": "2026-01-01T00:00:00Z"}],
                                                    note={"s": step, "t": "n%d" % step})}
        log = run["work_log"]
        self.assertEqual((len(log["calls"]), len(log["notes"])), (80, 40))
        self.assertEqual(log["calls"][0]["at"], "2026-01-01T00:00:00Z")
        self.assertTrue(log["notes"][-1]["at"] > "2026-01-02")

    def test_state_snapshot_includes_each_runs_work_log_with_stamps(self):
        run = self.engine.chat({"message": "Plan the launch", "agent_id": "a-lead", "channel": "dm-a-lead"})
        log = self.engine.work_log(run, calls=[{"s": 1, "n": "workspace.read", "a": "path=a.md", "r": "ok", "e": False}], note={"s": 1, "t": "Reading"})
        self.store.patch("runs", run["id"], {"work_log": log})
        sent = next(r for r in self.store.snapshot()["runs"] if r["id"] == run["id"])
        self.assertEqual(sent["work_log"]["notes"][0]["t"], "Reading")
        self.assertIn("at", sent["work_log"]["calls"][0])
        self.assertEqual(sent["root_id"], run["id"])   # the UI groups a room's runs by root_id


if __name__ == "__main__":
    unittest.main()
