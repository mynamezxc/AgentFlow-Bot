import json
import re
import time
import unittest
from unittest import mock
from unittest.mock import patch

import test_runtime as runtime
from agentflow.accounts import ProviderLimit
from agentflow.quota import PROBE_DELAYS, SHORT_DELAYS, TransientLimit
from agentflow.runtime import Engine
from agentflow.safety import LoopStopped
from agentflow.store import DomainError

CAP = "You've hit your usage limit."


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class QuotaBase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        patcher = patch("agentflow.quota.time.time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        runtime.RuntimeTests.setUp(self)
        self.fp = {p: self.providers.accounts.check(p, force=True)["fingerprint"] for p in ("codex", "claude")}
        self.providers.models = [
            {"id": "small", "provider": "codex", "name": "Small", "account_fingerprint": self.fp["codex"]},
            {"id": "large", "provider": "codex", "name": "Large", "account_fingerprint": self.fp["codex"]},
            {"id": "sonnet", "provider": "claude", "name": "Sonnet", "account_fingerprint": self.fp["claude"]},
            {"id": "opus", "provider": "claude", "name": "Opus", "account_fingerprint": self.fp["claude"]}]
        self.engine.models.set_default({"provider": "codex", "model": "small"})
        self.store.settings({"execution_enabled": True})
        self.service.action("agents", "a-seo", "model", {"provider": "claude", "model": "sonnet"})
        self.infer_calls = []

    tearDown = runtime.RuntimeTests.tearDown

    def work(self, agent_id, text="Build the report"):
        return self.engine.chat({"agent_id": agent_id, "message": text})

    def run_of(self, run):
        return self.store.get("runs", run["id"])

    def hit(self, run, provider="codex", text=CAP):
        with patch.object(self.providers, "infer", side_effect=ProviderLimit(provider, text)):
            self.engine.execute(run["id"])
        return self.run_of(run)

    def pump(self, limited=("codex",), results=None):
        def infer(actor, prompt, run_id):
            self.infer_calls.append((actor["provider"], run_id))
            if actor["provider"] in limited:
                raise ProviderLimit(actor["provider"], CAP)
            return (results or {}).get(run_id, '{"reply":"ok","state":"complete"}')
        with patch.object(self.providers, "infer", side_effect=infer):
            for future in self.engine.pump():
                future.result()

    def episodes(self):
        return list(self.engine.quota.episodes.values())

    def decisions(self):
        return [q for q in self.store.all("decisions") if q.get("quota_episode")]

    def usage(self, provider, used, reset=None, status=None):
        self.providers.usage.save(provider, self.fp[provider], [
            {"id": provider + ":primary", "used_percent": used, "resets_at": reset, "status": status}], "test")


class QuotaResumeTests(QuotaBase):
    def test_known_reset_from_error_text_resumes_at_reset_plus_30s(self):
        run = self.work("a-code")
        error = ProviderLimit("codex", "You've hit your limit · resets 10:30am (Asia/Ho_Chi_Minh)")
        reset = error.retry_at
        with patch.object(self.providers, "infer", side_effect=error):
            self.engine.execute(run["id"])
        saved = self.run_of(run)
        self.assertEqual(saved["status"], "waiting_quota")
        self.assertEqual(saved["retry_at"], reset + 30)
        self.assertEqual(saved["quota_provider"], "codex")
        self.assertTrue(saved["quota_episode"])
        self.clock.now = reset + 29
        self.engine.wake_waiters(check_accounts=False)
        self.assertEqual(self.run_of(run)["status"], "waiting_quota")
        self.clock.now = reset + 31
        self.engine.wake_waiters(check_accounts=False)
        self.assertEqual(self.run_of(run)["status"], "queued")
        self.assertEqual(self.episodes(), [])

    def test_known_reset_from_usage_windows_through_real_infer(self):
        self.usage("codex", 100, self.clock.now + 7200, "rejected")
        run = self.work("a-code")
        with patch.object(self.providers, "_infer", side_effect=ProviderLimit("codex", CAP)) as inference:
            self.engine.execute(run["id"])
        self.assertEqual(inference.call_count, 1)
        saved = self.run_of(run)
        self.assertEqual(saved["retry_at"], self.clock.now + 7230)
        stored = json.loads(self.providers.quota_path.read_text())
        self.assertEqual(stored[0]["retry_at"], self.clock.now + 7230)
        # a >=100% window without status counts too
        self.assertTrue(self.episodes()[0]["known"])

    def test_unknown_reset_backs_off_10_20_40_60_then_caps(self):
        run = self.work("a-code")
        delays = []
        with patch.object(self.providers, "_infer", side_effect=lambda *a: (_ for _ in ()).throw(ProviderLimit("codex", CAP))) as inference:
            self.engine.execute(run["id"])
            for _ in range(5):
                saved = self.run_of(run)
                self.assertEqual(saved["status"], "waiting_quota")
                self.assertIsNotNone(saved["retry_at"])
                delays.append(round(saved["retry_at"] - self.clock.now))
                self.clock.now = saved["retry_at"] + 1
                self.engine.wake_waiters(check_accounts=False)
                self.assertEqual(self.run_of(run)["status"], "queued")   # the probe is a real retry
                self.engine.execute(run["id"])
            self.assertEqual(inference.call_count, 6)
        self.assertEqual(delays, [600, 1200, 2400, 3600, 3600])
        self.assertEqual(PROBE_DELAYS, (600, 1200, 2400, 3600))

    def test_probe_time_uses_fresh_usage_before_spending_a_request(self):
        run = self.work("a-code")
        self.hit(run)
        self.clock.advance(601)
        self.usage("codex", 20)
        with patch.object(self.providers, "_infer") as inference:
            self.engine.wake_waiters(check_accounts=False)
        inference.assert_not_called()
        self.assertEqual(self.run_of(run)["status"], "queued")
        self.assertEqual(self.episodes(), [])

    def test_probe_time_still_exhausted_jumps_to_reported_reset(self):
        run = self.work("a-code")
        self.hit(run)
        self.clock.advance(601)
        reset = self.clock.now + 5000
        self.usage("codex", 100, reset, "rejected")
        self.engine.wake_waiters(check_accounts=False)
        saved = self.run_of(run)
        self.assertEqual(saved["status"], "waiting_quota")
        self.assertEqual(saved["retry_at"], reset + 30)
        self.assertTrue(self.episodes()[0]["known"])

    def test_stale_usage_does_not_end_the_wait_and_only_one_probe_is_released(self):
        first, second = self.work("a-code"), self.work("a-content")
        self.hit(first)
        self.pump()          # second is gated; no CLI
        self.assertEqual(self.run_of(second)["status"], "waiting_quota")
        self.clock.advance(601)
        self.usage("codex", 10)
        self.clock.advance(400)                      # data older than 5 minutes is not trusted
        self.engine.wake_waiters(check_accounts=False)
        states = {r["id"]: r["status"] for r in (self.run_of(first), self.run_of(second))}
        self.assertEqual(sorted(states.values()), ["queued", "waiting_quota"])
        self.assertEqual(self.episodes()[0]["phase"], "probing")

    def test_probe_success_releases_everyone(self):
        first, second = self.work("a-code"), self.work("a-content")
        self.hit(first)
        self.pump()
        self.clock.advance(601)
        self.engine.wake_waiters(check_accounts=False)
        probe = next(r for r in (self.run_of(first), self.run_of(second)) if r["status"] == "queued")
        with patch.object(self.providers, "_infer", return_value='{"reply":"ok","state":"complete"}'):
            self.providers.infer({"provider": "codex", "model": "small", "name": "Probe"}, "x", probe["id"])
        self.assertEqual(self.episodes(), [])
        self.engine.wake_waiters(check_accounts=False)
        self.assertTrue(all(self.run_of(r)["status"] == "queued" for r in (first, second)))

    def test_infer_ignores_and_drops_expired_blocks(self):
        key = ("codex", self.fp["codex"])
        legacy = ProviderLimit("codex", "old")
        legacy.retry_at = self.clock.now - 5
        self.providers.quota_blocks[key] = legacy
        actor = {"provider": "codex", "model": "small", "name": "Worker"}
        with patch.object(self.providers, "_infer", return_value="ok") as inference:
            self.assertEqual(self.providers.infer(actor, "x", "r1"), "ok")
        inference.assert_called_once()
        self.assertNotIn(key, self.providers.quota_blocks)
        self.assertEqual(json.loads(self.providers.quota_path.read_text()), [])
        none_block = ProviderLimit("codex", "legacy")
        self.providers.quota_blocks[key] = none_block
        with patch.object(self.providers, "_infer", return_value="ok") as inference:
            self.providers.infer(actor, "x", "r2")
        inference.assert_called_once()

    def test_account_switch_wakes_immediately(self):
        run = self.work("a-code")
        self.hit(run)
        self.assertEqual(self.run_of(run)["quota_account"], self.fp["codex"])
        self.providers.accounts.current["codex"] = {**self.providers.accounts.current["codex"], "fingerprint": "another-account"}
        self.engine.wake_waiters(check_accounts=False)
        self.assertEqual(self.run_of(run)["status"], "queued")
        self.assertEqual(self.episodes(), [])

    def test_episode_and_retry_survive_restart(self):
        run = self.work("a-code")
        self.hit(run)
        episode = dict(self.episodes()[0])
        self.engine.close()
        self.engine = Engine(self.store, self.providers, self.vault, self.root / "company")
        self.assertEqual(self.engine.quota.episodes[episode["id"]]["retry_at"], episode["retry_at"])
        self.engine.wake_waiters(check_accounts=False)
        self.assertEqual(self.run_of(run)["status"], "waiting_quota")
        self.clock.now = episode["retry_at"] + 1
        self.engine.wake_waiters(check_accounts=False)
        self.assertEqual(self.run_of(run)["status"], "queued")

    def test_legacy_waiting_run_without_retry_gets_a_schedule(self):
        run = self.work("a-code")
        self.store.patch("runs", run["id"], {"status": "waiting_quota", "retry_at": None, "quota_blocked_at": 1,
            "quota_binding": {"provider": "codex", "model": "small"}})
        self.engine.wake_waiters(check_accounts=False)
        saved = self.run_of(run)
        self.assertEqual(saved["status"], "waiting_quota")
        self.assertEqual(saved["retry_at"], self.clock.now + 600)
        self.clock.advance(601)
        self.engine.wake_waiters(check_accounts=False)
        self.assertEqual(self.run_of(run)["status"], "queued")

    def test_quota_wait_keeps_tool_checkpoint_and_resumes(self):
        run = self.work("a-code")
        outputs = [json.dumps({"reply": "Searching", "tool_calls": [{"name": "memory.search", "arguments": {"query": "x"}}]}), ProviderLimit("codex", CAP)]
        with patch.object(self.providers, "infer", side_effect=outputs):
            self.engine.execute(run["id"])
        saved = self.run_of(run)
        self.assertEqual(saved["status"], "waiting_quota")
        self.assertEqual(saved["steps"], 1)
        self.assertEqual(saved["phase"], "model")
        self.assertTrue(any(t["role"] == "tool" for t in saved["trace"]))
        self.assertTrue(any(t["role"] == "assistant" for t in saved["trace"]))
        self.clock.now = saved["retry_at"] + 1
        self.engine.wake_waiters(check_accounts=False)
        with patch.object(self.providers, "infer", return_value='{"reply":"Done","state":"complete"}'):
            self.engine.execute(run["id"])
        self.assertEqual(self.run_of(run)["status"], "done")
        self.assertEqual(self.run_of(run)["steps"], 2)

    def test_limit_does_not_consume_turn_budget(self):
        run = self.work("a-code")
        self.hit(run)
        self.assertEqual(self.run_of(run).get("inference_attempts", 0), 0)
        self.assertEqual(self.run_of(run).get("family_attempts", 0), 0)


class QuotaTeamTests(QuotaBase):
    def test_team_is_parked_without_spawning_clis_and_other_providers_continue(self):
        first, second, third, other = self.work("a-code"), self.work("a-content"), self.work("a-support"), self.work("a-seo")
        self.hit(first)
        self.pump()
        self.assertEqual(self.infer_calls, [("claude", other["id"])])
        self.assertEqual(self.run_of(other)["status"], "done")
        episode = self.episodes()[0]
        for run in (second, third):
            saved = self.run_of(run)
            self.assertEqual(saved["status"], "waiting_quota")
            self.assertEqual(saved["retry_at"], episode["retry_at"])
            self.assertEqual(saved["quota_episode"], episode["id"])
        action = self.store.get("agents", "a-content")["current_action"]
        self.assertRegex(action, r"^Waiting for Codex quota · (resumes|next check) \d\d:\d\d")
        clock = time.strftime("%H:%M", time.gmtime(episode["retry_at"] + 7 * 3600))
        self.assertIn(clock, action)
        # still parked on later pumps: nothing starts
        self.infer_calls.clear()
        self.pump()
        self.assertEqual(self.infer_calls, [])

    def test_exactly_one_decision_per_episode_updated_when_agents_join(self):
        first = self.work("a-code")
        self.hit(first)
        self.assertEqual(len(self.decisions()), 1)
        decision = self.decisions()[0]
        self.assertEqual((decision["channel"], decision["agent_id"], decision["blocking"]), ("dm-a-lead", "a-lead", False))
        self.assertEqual(decision["action_field"], "quota_action")
        self.assertEqual(decision["action_handler"], "quota")
        self.assertIn("1 task is paused", decision["question"])
        self.work("a-content")
        self.work("a-support")
        self.pump()
        self.assertEqual(len(self.decisions()), 1)
        updated = self.decisions()[0]
        self.assertEqual(updated["id"], decision["id"])
        self.assertIn("3 tasks are paused", updated["question"])
        message = next(m for m in self.store.all("messages") if m.get("decision_id") == decision["id"])
        self.assertIn("3 tasks are paused", message["content"])
        values = [a["value"] for a in updated["actions"]]
        self.assertEqual(values, ["wait", "switch_agent", "switch_all", "cancel"])
        self.assertTrue(updated["actions"][0]["label"].startswith("Wait — check again every 10–60 min"))
        self.assertIn("Sonnet", updated["actions"][1]["label"])
        self.assertTrue(updated["actions"][2]["label"].endswith("until Codex resets"))

    def test_known_reset_wait_label_and_no_switch_without_usable_fallback(self):
        self.providers.models = [m for m in self.providers.models if m["provider"] == "codex"]
        run = self.work("a-code")
        self.hit(run, text="limit · resets 10:30am (Asia/Ho_Chi_Minh)")
        decision = self.decisions()[0]
        self.assertEqual([a["value"] for a in decision["actions"]], ["wait", "cancel"])
        self.assertRegex(decision["actions"][0]["label"], r"^Wait — auto-resume at \d\d:\d\d")

    def test_wait_answer_keeps_waiting_and_does_not_spawn_followup(self):
        run = self.work("a-code")
        self.hit(run)
        decision = self.decisions()[0]
        before = len(self.store.all("runs"))
        self.engine.team.answer(decision["id"], {"answer": "Wait", "quota_action": "wait"})
        self.assertEqual(self.run_of(run)["status"], "waiting_quota")
        self.assertEqual(len(self.store.all("runs")), before)
        self.work("a-content")
        self.pump()
        self.assertEqual(len([q for q in self.decisions()]), 1)
        with self.assertRaises(DomainError):
            self.engine.team.answer(decision["id"], {"answer": "x", "quota_action": "nonsense"})

    def test_switch_agent_is_run_scoped_and_temporary(self):
        first, second = self.work("a-code"), self.work("a-content")
        self.hit(first)
        self.pump()
        decision = self.decisions()[0]
        self.engine.team.answer(decision["id"], {"answer": "Switch", "quota_action": "switch_agent"})
        for run in (first, second):
            saved = self.run_of(run)
            self.assertEqual(saved["status"], "queued")
            effective = self.engine.models.effective(self.store.get("agents", saved["agent_id"]), saved)
            self.assertEqual((effective["provider"], effective["model"], effective["source"]), ("claude", "sonnet", "quota"))
        self.assertEqual(self.store.settings()["default_provider"], "codex")
        self.pump(limited=())
        self.assertEqual({p for p, _ in self.infer_calls[-2:]}, {"claude"})
        # new work for the same agent still uses the normal binding (and is parked while the limit lasts)
        fresh = self.work("a-code", "Another task")
        self.assertEqual(self.engine.models.effective(self.store.get("agents", "a-code"), fresh)["provider"], "codex")
        self.infer_calls.clear()
        self.pump()
        self.assertEqual(self.infer_calls, [])
        self.assertEqual(self.run_of(fresh)["status"], "waiting_quota")

    def test_switch_all_changes_default_temporarily_and_restores_with_event(self):
        first, second = self.work("a-code"), self.work("a-content")
        self.hit(first, text="limit · resets 10:30am (Asia/Ho_Chi_Minh)")
        self.pump()
        decision = self.decisions()[0]
        self.engine.team.answer(decision["id"], {"answer": "Switch all", "quota_action": "switch_all"})
        settings = self.store.settings()
        self.assertEqual((settings["default_provider"], settings["default_model"]), ("claude", "sonnet"))
        for run in (first, second):
            self.assertEqual(self.run_of(run)["status"], "queued")
        self.clock.now = self.episodes()[0]["retry_at"] + 1
        self.engine.wake_waiters(check_accounts=False)
        settings = self.store.settings()
        self.assertEqual((settings["default_provider"], settings["default_model"]), ("codex", "small"))
        self.assertTrue(any(e["title"] == "Restored default model codex/small" for e in self.store.all("events")))
        self.assertIsNone(self.engine.quota.default_restore)

    def test_switch_all_does_not_overwrite_a_default_the_owner_chose_meanwhile(self):
        first = self.work("a-code")
        self.hit(first, text="limit · resets 10:30am (Asia/Ho_Chi_Minh)")
        self.engine.team.answer(self.decisions()[0]["id"], {"answer": "Switch all", "quota_action": "switch_all"})
        self.engine.models.set_default({"provider": "claude", "model": "opus"})
        self.clock.now = self.episodes()[0]["retry_at"] + 1
        self.engine.wake_waiters(check_accounts=False)
        self.assertEqual(self.store.settings()["default_model"], "opus")

    def test_cancel_action_cancels_blocked_work_only(self):
        first, second, other = self.work("a-code"), self.work("a-content"), self.work("a-seo")
        self.hit(first)
        self.pump()
        self.engine.team.answer(self.decisions()[0]["id"], {"answer": "Cancel", "quota_action": "cancel"})
        self.assertEqual(self.run_of(first)["status"], "cancelled")
        self.assertEqual(self.run_of(second)["status"], "cancelled")
        self.assertEqual(self.run_of(other)["status"], "done")

    def test_auto_fallback_policy_switches_without_asking(self):
        self.service.settings({"quota_policy": "auto_fallback", "fallback_models": [{"provider": "claude", "model": "opus"}]})
        first, second = self.work("a-code"), self.work("a-content")
        self.hit(first)
        saved = self.run_of(first)
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["model_override"], {"provider": "claude", "model": "opus"})
        self.assertEqual(self.decisions(), [])
        self.pump(limited=("codex",))
        self.assertEqual({p for p, _ in self.infer_calls}, {"claude"})
        self.assertEqual(self.run_of(second)["status"], "done")
        self.assertEqual(self.run_of(second)["model_override"], {"provider": "claude", "model": "opus"})

    def test_auto_fallback_derives_the_other_provider_when_none_configured(self):
        self.store.settings({"quota_policy": "auto_fallback"})
        run = self.work("a-code")
        self.hit(run)
        self.assertEqual(self.run_of(run)["model_override"]["provider"], "claude")

    def test_wait_policy_never_asks_and_never_switches(self):
        self.store.settings({"quota_policy": "wait", "fallback_models": [{"provider": "claude", "model": "opus"}]})
        run = self.work("a-code")
        self.hit(run)
        self.assertEqual(self.run_of(run)["status"], "waiting_quota")
        self.assertEqual(self.decisions(), [])

    def test_both_providers_exhausted_each_episode_resumes_independently(self):
        code, seo = self.work("a-code"), self.work("a-seo")
        self.hit(code, text="limit · resets 10:30am (Asia/Ho_Chi_Minh)")
        self.hit(seo, "claude", text="limit · resets 11:45pm (Asia/Ho_Chi_Minh)")
        codex_episode, claude_episode = (next(e for e in self.episodes() if e["provider"] == p) for p in ("codex", "claude"))
        self.assertEqual(len(self.decisions()), 2)
        by_provider = {q["quota_provider"]: q for q in self.decisions()}
        self.assertEqual([a["value"] for a in by_provider["codex"]["actions"]], ["wait", "cancel"])
        self.assertEqual([a["value"] for a in by_provider["claude"]["actions"]], ["wait", "cancel"])
        first_reset = min(codex_episode["retry_at"], claude_episode["retry_at"])
        first_provider = "codex" if codex_episode["retry_at"] == first_reset else "claude"
        self.clock.now = first_reset + 1
        self.engine.wake_waiters(check_accounts=False)
        statuses = {"codex": self.run_of(code)["status"], "claude": self.run_of(seo)["status"]}
        self.assertEqual(statuses.pop(first_provider), "queued")
        self.assertEqual(list(statuses.values()), ["waiting_quota"])

    def test_fallback_that_is_also_exhausted_just_waits(self):
        self.service.settings({"quota_policy": "auto_fallback", "fallback_models": [{"provider": "claude", "model": "opus"}]})
        seo = self.work("a-seo")
        self.hit(seo, "claude")
        code = self.work("a-code")
        self.hit(code)
        saved = self.run_of(code)
        self.assertEqual(saved["status"], "waiting_quota")
        self.assertNotIn("model_override", {k for k, v in saved.items() if v})

    def test_cancelling_root_clears_waiting_runs_and_closes_the_decision(self):
        run = self.work("a-code")
        self.hit(run)
        self.pump()
        self.engine.cancel(run["id"], cascade=True)
        self.assertEqual(self.run_of(run)["status"], "cancelled")
        self.engine.wake_waiters(check_accounts=False)
        self.assertEqual(self.store.get("decisions", self.decisions()[0]["id"])["status"], "cancelled")
        self.assertEqual(self.engine.quota.members(self.episodes()[0]), [])

    def test_stop_all_clears_waiting_quota_runs(self):
        first, second = self.work("a-code"), self.work("a-content")
        self.hit(first)
        self.pump()
        self.engine.safety.stop_all()
        for run in (first, second):
            self.assertEqual(self.run_of(run)["status"], "cancelled")
        self.engine.wake_waiters(check_accounts=False)
        self.assertEqual(self.engine.quota.members(self.episodes()[0]), [])


class RateLimitTests(QuotaBase):
    def test_transient_errors_retry_30s_2m_5m_then_fail_clearly(self):
        run = self.work("a-code")
        delays = []
        with patch.object(self.providers, "infer", side_effect=DomainError("The CLI reported overloaded_error", 529)):
            for _ in range(3):
                self.engine.execute(run["id"])
                saved = self.run_of(run)
                self.assertEqual(saved["status"], "retrying")
                delays.append(round(saved["retry_at"] - self.clock.now))
                self.clock.now = saved["retry_at"] + 1
                self.engine.wake_waiters(check_accounts=False)
                self.assertEqual(self.run_of(run)["status"], "queued")
            self.engine.execute(run["id"])
        self.assertEqual(delays, [30, 120, 300])
        self.assertEqual(SHORT_DELAYS, (30, 120, 300))
        saved = self.run_of(run)
        self.assertEqual(saved["status"], "failed")
        self.assertIn("after 3 retries", saved["error"])

    def test_rate_limit_text_is_not_a_quota_episode(self):
        actor = {"provider": "codex", "model": "small", "name": "Worker"}
        error = ProviderLimit("codex", "rate limit exceeded, please try again in 5s")
        with patch.object(self.providers, "_infer", side_effect=error):
            with self.assertRaises(TransientLimit):
                self.providers.infer(actor, "x", "r1")
        self.assertEqual(self.providers.quota_blocks, {})
        self.assertEqual(self.episodes(), [])
        overloaded = ProviderLimit("claude", "API Error: 529 overloaded_error")
        with patch.object(self.providers, "_infer", side_effect=overloaded):
            with self.assertRaises(TransientLimit):
                self.providers.infer({**actor, "provider": "claude", "model": "sonnet"}, "x", "r2")
        cap = ProviderLimit("codex", "You've hit your usage limit. Rate limit window resets later")
        with patch.object(self.providers, "_infer", side_effect=cap):
            with self.assertRaises(ProviderLimit):
                self.providers.infer(actor, "x", "r3")
        self.assertEqual(len(self.episodes()), 1)

    def test_transient_limit_drives_run_retry_through_execute(self):
        run = self.work("a-code")
        with patch.object(self.providers, "_infer", side_effect=ProviderLimit("codex", "429 too many requests")):
            self.engine.execute(run["id"])
        saved = self.run_of(run)
        self.assertEqual(saved["status"], "retrying")
        self.assertEqual(round(saved["retry_at"] - self.clock.now), 30)


class BudgetTests(QuotaBase):
    def halted_decision(self, root_id):
        return next(q for q in self.store.all("decisions") if q.get("safety_request") and q.get("safety_root") == root_id and q["status"] == "open")

    def test_defaults_and_validation(self):
        self.assertEqual(self.store.settings()["budget"], {"run_turns": 24, "project_turns": 64, "project_runs": 24, "project_tokens": 0})
        self.assertEqual(self.store.settings()["quota_policy"], "ask")
        for bad in ({"run_turns": 0}, {"run_turns": 501}, {"project_turns": 5001}, {"project_runs": 1001}, {"project_tokens": 10 ** 9 + 1},
                    {"run_turns": "5"}, {"run_turns": True}, {"unknown": 1}, "nope"):
            with self.assertRaises(DomainError, msg=str(bad)):
                self.service.settings({"budget": bad})
        result = self.service.settings({"budget": {"run_turns": 3, "project_tokens": 1000}})
        self.assertEqual(result["budget"], {"run_turns": 3, "project_turns": 64, "project_runs": 24, "project_tokens": 1000})
        with self.assertRaises(DomainError):
            self.service.settings({"quota_policy": "sometimes"})
        with self.assertRaises(DomainError):
            self.service.settings({"fallback_models": [{"provider": "claude", "model": "missing"}]})
        with self.assertRaises(DomainError):
            self.service.settings({"fallback_models": [{"provider": "claude", "model": "sonnet"}] * 1 + [{"provider": "codex", "model": "large"}] * 5})
        saved = self.service.settings({"fallback_models": [{"provider": "claude", "model": "sonnet"}, {"provider": "claude", "model": "sonnet"}]})
        self.assertEqual(saved["fallback_models"], [{"provider": "claude", "model": "sonnet"}])

    def test_run_turn_budget_comes_from_settings_with_generic_actions(self):
        self.store.settings({"budget": {"run_turns": 2, "project_turns": 64, "project_runs": 24, "project_tokens": 0}})
        run = self.work("a-code")
        with patch.object(self.providers, "infer", return_value=json.dumps({"reply": "step", "state": "continue"})) as model:
            self.engine.execute(run["id"])
        self.assertEqual(model.call_count, 2)
        root = self.run_of(run)
        self.assertTrue(root["safety_halted"])
        decision = self.halted_decision(run["id"])
        self.assertEqual(decision["action_field"], "safety_action")
        self.assertEqual(decision["action_handler"], "safety")
        self.assertEqual([a["value"] for a in decision["actions"]], ["continue", "allow_task", "cancel"])
        self.assertEqual(decision["actions"][0]["label"], "Continue (+2 turns)")
        self.assertEqual(decision["actions"][1]["label"], "Allow for this task until it's done")
        self.assertIn("2 model turns", decision["context"])

    def test_continue_adds_the_budget_again(self):
        self.store.settings({"budget": {"run_turns": 2, "project_turns": 64, "project_runs": 24, "project_tokens": 0}})
        run = self.work("a-code")
        with patch.object(self.providers, "infer", return_value=json.dumps({"reply": "step", "state": "continue", "tool_calls": [{"name": "memory.search", "arguments": {"query": str(time.time())}}]})):
            self.engine.execute(run["id"])
        decision = self.halted_decision(run["id"])
        self.engine.team.answer(decision["id"], {"answer": "Continue", "safety_action": "continue"})
        self.assertFalse(self.run_of(run)["safety_halted"])
        self.assertFalse(self.run_of(run).get("budget_unlimited"))
        self.assertEqual(self.run_of(run)["status"], "queued")

    def test_allow_task_skips_budgets_but_loop_brakes_stay_active(self):
        self.store.settings({"budget": {"run_turns": 2, "project_turns": 3, "project_runs": 1, "project_tokens": 5}})
        run = self.work("a-code")
        self.store.patch("runs", run["id"], {"family_tokens": 100})
        replies = [json.dumps({"reply": "step %d" % i, "state": "continue", "tool_calls": [{"name": "memory.search", "arguments": {"query": "q%d" % i}}]}) for i in range(40)]
        with patch.object(self.providers, "infer", side_effect=replies):
            self.engine.execute(run["id"])
        decision = self.halted_decision(run["id"])
        self.engine.team.answer(decision["id"], {"answer": "Allow", "safety_action": "allow_task"})
        root = self.run_of(run)
        self.assertTrue(root["budget_unlimited"])
        self.assertFalse(root["safety_halted"])
        # budgets (turns, runs and tokens) no longer apply to this root and its children
        child = self.engine.enqueue("a-content", "More", "peer", root_id=run["id"])
        self.engine.enqueue("a-seo", "More", "peer", root_id=run["id"])
        for _ in range(6):
            self.engine.safety.before_infer(child)
        self.assertFalse(self.run_of(run)["safety_halted"])
        # the repeated-operation brake is still on, and offers no unlimited option
        call = {"name": "workspace.read", "arguments": {"path": "missing.md"}}
        with self.assertRaises(LoopStopped):
            for _ in range(3):
                self.engine.safety.tool_result(self.run_of(run), call, {"error": "same"})
        loop = self.halted_decision(run["id"])
        self.assertEqual([a["value"] for a in loop["actions"]], ["continue", "cancel"])
        self.assertIn("same tool", loop["context"])
        self.assertIn("loop brake", loop["question"])
        with self.assertRaises(DomainError):
            self.engine.team.answer(loop["id"], {"answer": "x", "safety_action": "allow_task"})

    def test_project_token_budget_uses_family_tokens_and_continue_resets_baseline(self):
        self.store.settings({"budget": {"run_turns": 24, "project_turns": 64, "project_runs": 24, "project_tokens": 1000}})
        run = self.work("a-code")
        self.store.patch("runs", run["id"], {"family_tokens": 999})
        self.engine.safety.before_infer(self.run_of(run))                      # still below the limit
        self.store.patch("runs", run["id"], {"family_tokens": 1000})
        with self.assertRaises(LoopStopped):
            self.engine.safety.before_infer(self.run_of(run))
        decision = self.halted_decision(run["id"])
        self.assertEqual(decision["actions"][0]["label"], "Continue (+1,000 tokens)")
        self.engine.team.answer(decision["id"], {"answer": "Continue", "safety_action": "continue"})
        self.engine.safety.before_infer(self.run_of(run))                      # baseline moved
        self.store.patch("runs", run["id"], {"family_tokens": 2100})
        with self.assertRaises(LoopStopped):
            self.engine.safety.before_infer(self.run_of(run))

    def test_missing_family_tokens_counts_as_zero_and_zero_means_unlimited(self):
        self.store.settings({"budget": {"run_turns": 24, "project_turns": 64, "project_runs": 24, "project_tokens": 10}})
        run = self.work("a-code")
        self.engine.safety.before_infer(self.run_of(run))
        self.store.settings({"budget": {"run_turns": 24, "project_turns": 64, "project_runs": 24, "project_tokens": 0}})
        self.store.patch("runs", run["id"], {"family_tokens": 10 ** 8})
        self.engine.safety.before_infer(self.run_of(run))

    def test_project_runs_budget_and_cancel_action(self):
        self.store.settings({"budget": {"run_turns": 24, "project_turns": 64, "project_runs": 2, "project_tokens": 0}})
        run = self.work("a-code")
        self.engine.enqueue("a-content", "One", "peer", root_id=run["id"])
        with self.assertRaises(LoopStopped):
            self.engine.enqueue("a-seo", "Two", "peer", root_id=run["id"])
        decision = self.halted_decision(run["id"])
        self.assertEqual(decision["actions"][0]["label"], "Continue (+2 runs)")
        self.engine.team.answer(decision["id"], {"answer": "Cancel", "safety_action": "cancel"})
        self.assertEqual(self.run_of(run)["status"], "cancelled")

    def test_legacy_safety_decisions_without_actions_still_work(self):
        run = self.work("a-code")
        self.store.patch("runs", run["id"], {"safety_halted": True, "safety_reason": "old"})
        approval = self.engine.enqueue("a-lead", "Review", "safety_approval", root_id=run["id"], channel="dm-a-lead", status="waiting_user", system_managed=True)
        asked = self.engine.team.ask(self.store.get("agents", "a-lead"), {"question": "Old question", "options": ["Continue", "Cancel"], "blocking": True}, approval, _routed=True)
        self.store.patch("decisions", asked["decision_id"], {"safety_request": True, "safety_root": run["id"]})
        with self.assertRaises(DomainError):
            self.engine.team.answer(asked["decision_id"], {"answer": "x", "safety_action": "allow_task"})
        self.engine.team.answer(asked["decision_id"], {"answer": "Continue", "safety_action": "continue"})
        self.assertFalse(self.run_of(run)["safety_halted"])


if __name__ == "__main__":
    unittest.main()


class TypedActionAnswerTests(unittest.TestCase):
    def test_typed_label_or_number_selects_action(self):
        from agentflow.collaboration import Collaboration
        handled = []
        engine = mock.Mock(); engine.decision_handlers = {"h": lambda q, a: handled.append(a["x_action"])}
        team = Collaboration.__new__(Collaboration); team.engine = engine
        question = {"actions": [{"value": "wait", "label": "Wait"}, {"value": "cancel", "label": "Cancel"}],
                    "action_field": "x_action", "action_handler": "h"}
        import contextlib
        store = mock.Mock(); store.transaction.return_value = contextlib.nullcontext()
        store.get.side_effect = lambda kind, key: {**question, "id": "d", "status": "open", "run_id": "r", "agent_id": "a",
            "channel": "c", "blocking": True, "root_id": "r"} if kind == "decisions" else {"status": "waiting_user"}
        team.store = store
        team.answer("d", {"answer": "2"})
        self.assertEqual(handled, ["cancel"])
