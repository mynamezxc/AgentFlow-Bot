import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from agentflow.prompting import (CHECKPOINT_CHARS, DELTA_LIMIT, RESULT_LIMIT, clip_batch, clip_result, clip_trace_content, gist, usage_record)
from agentflow.sessions import UsageSemantics, codex_turn, estimate_cumulative_total, migrate_codex_usage
from test_tokens import FINAL, FakeCli, TokenTests, tool_turn

ROOT = Path(__file__).resolve().parents[1]
TRUNC = "…[truncated "


class FlatCodex(FakeCli):
    """A codex CLI whose turn.completed.usage is PER TURN (the guard case): first turn reports more than the second."""
    def cumulative(self, thread):
        self.turn = getattr(self, "turn", 0) + 1
        return {"input_tokens": 2000 if self.turn == 1 else 1000, "cached_input_tokens": 800, "output_tokens": 50}


class DietTests(unittest.TestCase):
    # Reuse the fake-CLI harness of test_tokens without inheriting (and re-running) its tests.
    setUp = TokenTests.setUp
    tearDown = TokenTests.tearDown
    configure, busy_project, work_run, actor, run_with = (TokenTests.configure, TokenTests.busy_project, TokenTests.work_run,
                                                          TokenTests.actor, TokenTests.run_with)

    def models_with_medium(self):
        for entry in self.providers.models:
            if entry["provider"] in ("claude", "codex") and entry["reasoning_efforts"]:
                entry["reasoning_efforts"] = ["low", "medium", "high", "xhigh"]

    # ---- 1. Codex accounting ----------------------------------------------------------------------------------
    def test_codex_cumulative_usage_becomes_per_turn_deltas_and_is_recorded_per_turn(self):
        run, fake = self.run_with("codex", [tool_turn(query="a"), tool_turn(query="b"), FINAL])
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(run["token_usage"], {"input_tokens": 600, "cached_input_tokens": 2400, "cache_creation_tokens": 0, "output_tokens": 150, "turns": 3})
        self.assertEqual(self.store.get("runs", run["root_id"])["family_tokens"], 600 + 150)
        turns = [h["usage"] for h in run["model_history"]]
        self.assertEqual(turns, [{"input": 200, "cached": 800, "cache_write": 0, "output": 50, "context": 1000}] * 3)
        self.assertEqual(run["native_session"]["usage_seen"], {"input_tokens": 3000, "cached_input_tokens": 2400, "output_tokens": 150})
        self.assertEqual(run["native_session"]["last_context"], 1000)
        self.assertEqual(self.providers.usage_semantics.mode("codex"), "cumulative")

    def test_claude_usage_is_not_subtracted_and_records_context(self):
        run, _ = self.run_with("claude", [tool_turn(query="a"), FINAL])
        self.assertEqual(run["model_history"][1]["usage"], {"input": 50, "cached": 900, "cache_write": 100, "output": 20, "context": 1050})
        self.assertEqual(run["native_session"]["last_context"], 1050)
        self.assertNotIn("usage_seen", run["native_session"])

    def test_first_resumed_turn_with_smaller_input_marks_codex_as_per_turn_and_persists(self):
        run = self.work_run("codex")
        fake = FlatCodex("codex", [tool_turn(query="a"), tool_turn(query="b"), FINAL])
        with patch("agentflow.providers.subprocess.Popen", side_effect=fake):
            self.engine.execute(run["id"])
        done = self.store.get("runs", run["id"])
        self.assertEqual(self.providers.usage_semantics["codex"], "per_turn")
        self.assertEqual(done["token_usage"]["input_tokens"], (1200 + 200 + 200))  # nothing was subtracted
        self.assertEqual(json.loads((self.store.directory / "usage-semantics.json").read_text()), {"codex": "per_turn"})
        self.assertEqual(UsageSemantics(self.store.directory).mode("codex"), "per_turn")

    def test_codex_turn_unit(self):
        semantics = UsageSemantics(self.root)
        raw = lambda i, c, o: {"input_tokens": i, "cached_input_tokens": c, "output_tokens": o}
        self.assertEqual(codex_turn(raw(500, 100, 10), None, 0, semantics), (raw(500, 100, 10), raw(500, 100, 10)))
        self.assertEqual(codex_turn(raw(900, 300, 30), raw(500, 100, 10), 1, semantics)[0], raw(400, 200, 20))
        # a component that went down: treated as a fresh thread
        self.assertEqual(codex_turn(raw(900, 50, 30), raw(500, 100, 10), 2, semantics)[0], raw(900, 50, 30))
        self.assertEqual(semantics.mode("codex"), "cumulative")
        self.assertEqual(codex_turn(raw(100, 0, 5), raw(500, 100, 10), 1, semantics)[0], raw(100, 0, 5))
        self.assertEqual(semantics["codex"], "per_turn")
        self.assertEqual(codex_turn(raw(900, 300, 30), raw(500, 100, 10), 2, semantics)[0], raw(900, 300, 30))

    def test_migration_flags_codex_runs_and_estimates_a_conservative_total(self):
        run, _ = self.run_with("codex", [tool_turn(query="a"), FINAL])
        root_id = run["root_id"]
        inflated = {"input_tokens": 1_000_000, "cached_input_tokens": 9_000_000, "cache_creation_tokens": 0, "output_tokens": 20_000, "turns": 9}
        self.store.patch("runs", run["id"], {"token_usage": inflated, "native_session": {**run["native_session"], "resets": 0}})
        self.store.patch("runs", root_id, {"family_tokens": 1_020_000, "family_cached_tokens": 9_000_000})
        claude = self.store.create("runs", {"agent_id": "a-lead", "status": "done", "kind": "chat", "root_id": root_id, "provider": "claude",
                                            "token_usage": {"input_tokens": 5, "cached_input_tokens": 5, "cache_creation_tokens": 0, "output_tokens": 5, "turns": 3},
                                            "model_history": [{"provider": "claude"}] * 3, "native_session": {"id": "x" * 12, "turns": 3}})
        (self.store.directory / "token-accounting.json").unlink(missing_ok=True)
        result = migrate_codex_usage(self.store)
        self.assertEqual(result["runs"], 1)
        fixed = self.store.get("runs", run["id"])
        self.assertTrue(fixed["token_usage_overcounted"])
        self.assertEqual(fixed["token_usage_raw"], inflated)
        self.assertEqual(fixed["token_usage"]["input_tokens"], estimate_cumulative_total(1_000_000, 9))  # 2*S/(n+1) = 200_000
        self.assertEqual(fixed["token_usage"]["input_tokens"], 200_000)
        self.assertEqual(fixed["token_usage"]["turns"], 9)
        root = self.store.get("runs", root_id)
        self.assertEqual(root["family_tokens"], 1_020_000 - (800_000 + 16_000))
        self.assertEqual(root["family_tokens_raw"], 1_020_000)
        self.assertNotIn("token_usage_overcounted", self.store.get("runs", claude["id"]))
        self.assertEqual(migrate_codex_usage(self.store), {})  # one-time
        self.assertEqual(self.store.get("runs", root_id)["family_tokens"], root["family_tokens"])
        self.assertEqual(estimate_cumulative_total(1000, 1), 1000)
        self.assertEqual(estimate_cumulative_total(6000, 4, segments=2), 4000)  # two threads of two turns: 2*3000/3 each

    # ---- 2. compaction -----------------------------------------------------------------------------------------
    def session_run(self, **state):
        run, _ = self.run_with("claude", [tool_turn(query="x"), FINAL])
        return self.store.get("runs", run["id"]), state

    def test_context_over_threshold_or_many_turns_restarts_with_a_compact_checkpoint(self):
        done, _ = self.session_run()
        actor = self.actor(done)
        session = done["native_session"]
        self.assertIsNotNone(self.engine.prompt(actor, done).delta)
        self.assertEqual(self.store.settings().get("session_compact_tokens", 48000), 48000)
        big = {**done, "native_session": {**session, "last_context": 47000, "turns": 5, "base_context": 20000}}
        self.assertIsNotNone(self.engine.prompt(actor, big).delta)
        big["native_session"]["last_context"] = 48000
        bundle = self.engine.prompt(actor, big)
        self.assertIsNone(bundle.delta)
        self.assertEqual(bundle.stats["compacted"], "context")
        self.assertIn("YOUR CHECKPOINT: (compacted", bundle.volatile)
        self.store.settings({"session_compact_tokens": 60000})
        self.assertIsNotNone(self.engine.prompt(actor, big).delta)
        self.store.settings({"session_compact_tokens": 1})  # clamped to 16000
        self.assertEqual(self.engine.builder().compact_threshold(), 16000)
        self.store.settings({"session_compact_tokens": 48000})
        many = {**done, "native_session": {**session, "last_context": 100, "turns": 31}}
        self.assertEqual(self.engine.prompt(actor, many).stats["compacted"], "turns")
        self.assertIsNotNone(self.engine.prompt(actor, {**done, "native_session": {**session, "last_context": 100, "turns": 30}}).delta)
        young = {**done, "native_session": {**session, "last_context": 90000, "turns": 1}}  # a fresh session cannot shrink by restarting
        self.assertIsNotNone(self.engine.prompt(actor, young).delta)
        hard = {**done, "native_session": {**session, "turns": 41}}
        self.assertIsNone(self.engine.prompt(actor, hard).delta)

    def test_compaction_is_applied_in_a_live_run_and_session_restarts(self):
        run = self.work_run("claude")
        fake = FakeCli("claude", [tool_turn(query="x"), tool_turn(query="y"), FINAL])
        fake.usage = {"input_tokens": 10, "cache_read_input_tokens": 20000, "cache_creation_input_tokens": 100, "output_tokens": 20}
        original = self.providers.infer

        def infer(agent, prompt, run_id):
            if len(fake.calls) == 1:
                fake.usage = {"input_tokens": 10, "cache_read_input_tokens": 60000, "cache_creation_input_tokens": 100, "output_tokens": 20}
            return original(agent, prompt, run_id)
        with patch("agentflow.providers.subprocess.Popen", side_effect=fake), patch.object(self.providers, "infer", side_effect=infer):
            self.engine.execute(run["id"])
        done = self.store.get("runs", run["id"])
        # turn 2 measured 60k >= 48k, so turn 3 starts a fresh session with the compact checkpoint instead of resuming
        self.assertEqual([c["resumed"] for c in fake.calls], [False, True, False])
        self.assertIn("--session-id", fake.calls[2]["args"])
        self.assertIn("compacted", fake.calls[2]["stdin"])
        self.assertEqual(done["native_session"]["turns"], 1)
        self.assertEqual(done["native_session"]["resets"], 1)

    def big_run(self):
        run = self.work_run("claude")
        batches = []
        for n in range(6):
            batches += [{"role": "assistant", "content": json.dumps({"reply": f"step {n}", "progress": f"Reading part {n}", "state": "continue",
                         "tool_calls": [{"name": "workspace.read", "arguments": {"path": f"doc{n}.md"}}, {"name": "web.fetch", "arguments": {"url": f"https://x.test/{n}"}}]})},
                        {"role": "tool", "content": json.dumps([{"tool": "workspace.read", "result": f"R{n}-" + "line of text\n" * 6000},
                                                                {"tool": "web.fetch", "result": {"title": f"Page {n}", "text": "w" * 50000}}])}]
        calls = [{"s": n, "n": "workspace.read", "a": f"path=doc{n}.md", "r": "ok " + "x" * 100, "e": n % 7 == 0} for n in range(80)]
        return self.store.patch("runs", run["id"], {"trace": batches, "summary": "S" * 3000, "last_reply": "L" * 2000,
                                                    "work_log": {"calls": calls, "notes": [{"s": n, "t": "N" * 300} for n in range(40)]}})

    def test_compact_checkpoint_is_bounded_and_keeps_work_so_far_plus_last_two_batches(self):
        run = self.big_run()
        text = self.engine.builder().compact_checkpoint(run)
        self.assertLessEqual(len(text), CHECKPOINT_CHARS)
        self.assertIn("work_so_far", text)
        self.assertIn("progress_notes", text)
        self.assertIn("R5-", text)
        self.assertIn("R4-", text)
        self.assertNotIn("R3-", text)
        self.assertIn("ERR", text)
        self.assertIn("earlier_tool_calls", text)
        bundle = self.engine.prompt(self.actor(run), run)
        self.assertLessEqual(bundle.stats["checkpoint_chars"], CHECKPOINT_CHARS)
        self.assertLess(len(bundle.volatile) - bundle.stats["checkpoint_chars"], 60000)
        # small traces keep their results readable (each result clipped to 8k, total bound honoured)
        small = {**run, "trace": run["trace"][-4:], "work_log": None}
        again = self.engine.builder().compact_checkpoint(small)
        self.assertLessEqual(len(again), CHECKPOINT_CHARS)
        self.assertIn("tool_calls", again)  # derived from the trace for legacy runs

    def test_est_tokens_counts_images_and_prefers_the_measured_context(self):
        run = self.work_run("claude")
        bundle = self.engine.prompt(self.actor(run), run)
        info = {"resumed": False, "session_id": "s" * 12, "sent_chars": 3500, "image_keys": ["a", "b"]}
        patch_ = self.engine.native_patch(run, bundle, {"native": info}, "x" * 350)
        self.assertEqual(patch_["native_session"]["est_tokens"], 1100 + 2 * 1600)
        usage = {"input_tokens": 100, "cached_input_tokens": 9000, "cache_creation_tokens": 900, "output_tokens": 50}
        measured = self.engine.native_patch(run, bundle, {"native": info, "usage": usage}, "x")
        self.assertEqual(measured["native_session"]["est_tokens"], 10000 + 50)
        self.assertEqual(measured["native_session"]["last_context"], 10000)

    # ---- 3. clipping --------------------------------------------------------------------------------------------
    def test_clip_result_keeps_shape_and_ends_with_the_marker(self):
        text = clip_result("workspace.read", "a" * 30000)
        self.assertLessEqual(len(text), RESULT_LIMIT)
        self.assertTrue(text.endswith("chars — re-run with offset/limit or a narrower query]"))
        self.assertIn(TRUNC, text)
        self.assertEqual(clip_result("x", "short"), "short")
        record = {"path": "a.md", "content": "c" * 40000, "bytes": 40000, "items": [{"t": "t" * 5000}, {"t": "u" * 5000}]}
        clipped = clip_result("project.read", record)
        self.assertLessEqual(len(json.dumps(clipped, ensure_ascii=False, separators=(",", ":"))), RESULT_LIMIT)
        self.assertEqual((clipped["path"], clipped["bytes"], len(clipped["items"])), ("a.md", 40000, 2))
        self.assertIn(TRUNC, clipped["content"])
        self.assertGreater(len(clipped["content"]), 3000)  # water-filling: the big strings share the cut
        small = {"ok": True, "n": 3}
        self.assertIs(clip_result("x", small), small)
        many = {"rows": [{"id": i, "v": "v" * 20} for i in range(2000)]}
        self.assertLessEqual(len(json.dumps(clip_result("x", many))), RESULT_LIMIT + 400)

    def test_batch_cap_reduces_the_oldest_results_to_a_gist(self):
        results = [{"tool": "workspace.read", "result": "o" * 12000}, {"tool": "web.fetch", "result": {"text": "m" * 12000}},
                   {"tool": "browser.open", "result": {"snapshot": "n" * 12000}}, {"tool": "workspace.read", "result": "z" * 12000}]
        batch = clip_batch(results)
        total = len(json.dumps(batch, ensure_ascii=False, separators=(",", ":")))
        self.assertLessEqual(total, DELTA_LIMIT)
        self.assertIn("gist", batch[0]["result"])
        self.assertIsInstance(batch[3]["result"], str)  # newest results stay in full
        self.assertTrue(batch[3]["result"].startswith("zzz"))
        self.assertLessEqual(len(gist({"error": "e" * 500})), 160)
        self.assertTrue(gist({"error": "boom"}).startswith("error: boom"))
        self.assertIn("read", gist("read ok\nline 2"))
        stored = json.dumps([{"tool": "x", "result": "q" * 90000}])
        self.assertLessEqual(len(clip_trace_content(stored)), DELTA_LIMIT)
        self.assertEqual(clip_trace_content("short result"), "short result")

    def test_tool_results_are_clipped_in_trace_and_delta_but_tool_events_stay_full(self):
        self.configure("claude")
        run = self.engine.chat({"message": "Prepare the launch report", "agent_id": "a-lead", "channel": "dm-a-lead"})
        self.busy_project(run)
        big = "B" * 30000
        original = self.engine.tool

        def tool(actor, call, run):
            return {"content": big, "path": "huge.md"} if call["name"] == "memory.search" else original(actor, call, run)
        fake = FakeCli("claude", [tool_turn(query="x"), FINAL])
        with patch("agentflow.providers.subprocess.Popen", side_effect=fake), patch.object(self.engine, "tool", side_effect=tool):
            self.engine.execute(run["id"])
        done = self.store.get("runs", run["id"])
        batch = json.loads(next(e["content"] for e in done["trace"] if e["role"] == "tool"))
        self.assertLessEqual(len(batch[0]["result"]["content"]), RESULT_LIMIT)
        self.assertIn(TRUNC, fake.calls[1]["stdin"])
        self.assertLess(len(fake.calls[1]["stdin"]), 20000)
        event = next(e for e in self.store.all("tool_events") if e["run_id"] == run["id"])
        self.assertEqual(event["result"]["content"], big)
        self.assertEqual(done["work_log"]["calls"][0]["n"], "memory.search")

    # ---- 4. read dedupe ------------------------------------------------------------------------------------------
    def test_unchanged_rereads_in_the_same_session_return_a_stub_but_events_keep_content(self):
        self.configure("claude")
        run = self.engine.chat({"message": "Prepare the launch report", "agent_id": "a-lead", "channel": "dm-a-lead"})
        self.busy_project(run)
        self.engine.tool(self.store.get("agents", "a-lead"), {"name": "workspace.write", "arguments": {"path": "notes/a.md", "content": "alpha " * 50}}, run)
        stamp = [100]
        fingerprint = lambda args, project: (str(Path("/ws") / args["path"]), 300, stamp[0]) if args.get("path") else None
        read = lambda: tool_turn("workspace.read", path="notes/a.md")
        fake = FakeCli("claude", [read(), read(), read(), FINAL])
        original = self.providers.infer

        def infer(agent, prompt, run_id):
            if len(fake.calls) == 2:
                stamp[0] = 200  # the file changes before the third read
                self.engine.tool(self.store.get("agents", "a-lead"), {"name": "workspace.write", "arguments": {"path": "notes/a.md", "content": "alpha beta " * 40}}, run)
            return original(agent, prompt, run_id)
        with patch.object(self.engine.files, "fingerprint", fingerprint, create=True), patch("agentflow.providers.subprocess.Popen", side_effect=fake), \
                patch.object(self.providers, "infer", side_effect=infer):
            self.engine.execute(run["id"])
        done = self.store.get("runs", run["id"])
        batches = [json.loads(e["content"]) for e in done["trace"] if e["role"] == "tool"]
        self.assertIn("alpha", json.dumps(batches[0]))
        self.assertEqual(batches[1][0]["result"]["unchanged"], True)
        self.assertIn("Identical to your read at turn 1", batches[1][0]["result"]["note"])
        self.assertIn("alpha", json.dumps(batches[2]))  # changed file: full content again
        self.assertIn("unchanged", fake.calls[2]["stdin"])
        events = [e for e in self.store.all("tool_events") if e["run_id"] == run["id"] and e["name"] == "workspace.read"]
        self.assertTrue(all("alpha" in json.dumps(e["result"]) for e in events))
        self.assertEqual(len(done["native_session"]["reads"]), 2)

    def test_read_map_resets_with_the_session_and_is_skipped_when_a_reset_is_imminent(self):
        done, _ = self.session_run()
        session = {**done["native_session"], "reads": {"k": 1}}
        bundle = self.engine.prompt(self.actor(done), {**done, "native_session": session})
        patched = self.engine.native_patch({**done, "native_session": session}, bundle, {"native": {"resumed": False, "session_id": "n" * 12, "sent_chars": 10}}, "x")
        self.assertEqual(patched["native_session"]["reads"], {})
        kept = self.engine.native_patch({**done, "native_session": session}, bundle, {"native": {"resumed": True, "session_id": "n" * 12, "sent_chars": 10}}, "x")
        self.assertEqual(kept["native_session"]["reads"], {"k": 1})
        self.assertFalse(self.engine.builder().will_reset({**done, "native_session": session}))
        self.assertTrue(self.engine.builder().will_reset({**done, "native_session": {**session, "last_context": 99999, "turns": 4}}))
        self.assertTrue(self.engine.builder().will_reset({**done, "native_session": None}))
        call = {"name": "workspace.read", "arguments": {"path": "x.md"}}
        with patch.object(self.engine.files, "fingerprint", lambda a, p: ("/ws/x.md", 1, 1), create=True):
            self.store.patch("runs", done["id"], {"native_session": {**session, "last_context": 99999, "turns": 4}})
            self.assertEqual(self.engine.dedupe_read(call, done, "content"), "content")
            self.assertEqual(self.store.get("runs", done["id"])["native_session"]["reads"], {"k": 1})

    def test_dedupe_is_skipped_without_fingerprint_and_for_errors_and_large_results(self):
        done, _ = self.session_run()
        call = {"name": "workspace.read", "arguments": {"path": "x.md"}}
        self.assertEqual(self.engine.dedupe_read(call, done, "text"), "text")  # files.fingerprint not available yet
        with patch.object(self.engine.files, "fingerprint", lambda a, p: ("/ws/x.md", 1, 1), create=True):
            self.assertEqual(self.engine.dedupe_read(call, done, {"error": "nope"}), {"error": "nope"})
            self.assertEqual(self.engine.dedupe_read(call, done, "x" * (RESULT_LIMIT + 1)), "x" * (RESULT_LIMIT + 1))
            self.assertEqual(self.store.get("runs", done["id"])["native_session"].get("reads", {}), {})
            self.assertEqual(self.engine.dedupe_read(call, done, "text"), "text")
            stub = self.engine.dedupe_read(call, self.store.get("runs", done["id"]), "text")
            self.assertTrue(stub["unchanged"])

    # ---- 5. effort ----------------------------------------------------------------------------------------------
    def test_effort_change_keeps_the_native_session_and_is_sent_per_call(self):
        self.configure("claude")
        self.models_with_medium()
        run = self.engine.chat({"message": "Prepare the launch report", "agent_id": "a-lead", "channel": "dm-a-lead"})
        self.busy_project(run)
        fake = FakeCli("claude", [tool_turn(query="x"), FINAL])
        original = self.providers.infer

        def infer(agent, prompt, run_id):
            if fake.calls:
                self.store.patch("agents", "a-lead", {"reasoning_effort": "high"})
                agent = {**agent, "reasoning_effort": "high"}
            return original(agent, prompt, run_id)
        with patch("agentflow.providers.subprocess.Popen", side_effect=fake), patch.object(self.providers, "infer", side_effect=infer):
            self.engine.execute(run["id"])
        first, second = fake.calls
        self.assertFalse(second["resumed"] is False)
        self.assertEqual(second["args"][second["args"].index("--resume") + 1], first["args"][first["args"].index("--session-id") + 1])
        self.assertNotIn("--effort", first["args"])
        self.assertEqual(second["args"][second["args"].index("--effort") + 1], "high")

    def adaptive_state(self, calls, effort="high", source="agent", error=False, **extra):
        self.configure("claude")
        self.models_with_medium()
        run = self.engine.chat({"message": "Prepare the launch report", "agent_id": "a-lead", "channel": "dm-a-lead"})
        result = {"error": "bad"} if error else {"ok": True}
        trace = [{"role": "assistant", "content": json.dumps({"reply": "r", "state": "continue", "tool_calls": [{"name": n, "arguments": {}} for n in calls]})},
                 {"role": "tool", "content": json.dumps([{"tool": n, "result": result} for n in calls])}] if calls else []
        run = {**self.store.get("runs", run["id"]), "trace": trace, **extra}
        selected = {"provider": "claude", "model": "sonnet", "reasoning_effort": effort, "source": source}
        return self.engine.builder().adaptive_effort(run, selected)

    def test_adaptive_effort_lowers_one_level_after_mechanical_tool_batches(self):
        self.assertEqual(self.adaptive_state(["browser.open", "workspace.read", "memory.search", "progress.update"]), ("medium", True))
        self.assertEqual(self.adaptive_state(["web.fetch", "outputs.view_image", "tools.inspect", "project.search"], effort="xhigh"), ("high", True))
        self.assertEqual(self.adaptive_state(["browser.act"], effort="max"), ("xhigh", True))
        self.assertEqual(self.adaptive_state(["workspace.read", "workspace.write"]), ("high", False))
        self.assertEqual(self.adaptive_state(["workspace.read"], error=True), ("high", False))
        self.assertEqual(self.adaptive_state([]), ("high", False))
        self.assertEqual(self.adaptive_state(["workspace.read"], effort="medium"), ("medium", False))
        self.assertEqual(self.adaptive_state(["workspace.read"], source="run"), ("high", False))  # owner/agent pinned per-run override
        self.assertEqual(self.adaptive_state(["workspace.read"], source="quota"), ("high", False))
        self.assertEqual(self.adaptive_state(["workspace.read"], effort_pinned=True), ("high", False))
        self.store.settings({"adaptive_effort": False})
        self.assertEqual(self.adaptive_state(["workspace.read"]), ("high", False))

    def test_adaptive_effort_needs_the_lower_level_in_the_models_efforts(self):
        self.configure("claude")
        run = {**self.store.get("runs", self.engine.chat({"message": "Hi there", "agent_id": "a-lead", "channel": "dm-a-lead"})["id"]),
               "trace": [{"role": "assistant", "content": json.dumps({"tool_calls": [{"name": "workspace.read", "arguments": {}}]})}]}
        selected = {"provider": "claude", "model": "sonnet", "reasoning_effort": "high", "source": "agent"}
        self.assertEqual(self.engine.builder().adaptive_effort(run, selected), ("high", False))  # model lists only low/high

    def test_effort_used_is_recorded_in_model_history_and_sent_to_the_cli(self):
        self.configure("claude")
        self.models_with_medium()
        self.store.patch("agents", "a-lead", {"reasoning_effort": "high"})
        run = self.engine.chat({"message": "Prepare the launch report", "agent_id": "a-lead", "channel": "dm-a-lead"})
        self.busy_project(run)
        fake = FakeCli("claude", [tool_turn("workspace.search", query="x"), tool_turn("workspace.write", path="a.md", content="c"), FINAL])
        with patch("agentflow.providers.subprocess.Popen", side_effect=fake):
            self.engine.execute(run["id"])
        done = self.store.get("runs", run["id"])
        self.assertEqual([h["effort_used"] for h in done["model_history"][:3]], ["high", "medium", "high"])
        self.assertEqual([h["reasoning_effort"] for h in done["model_history"][:3]], ["high"] * 3)
        self.assertTrue(all(c["resumed"] for c in fake.calls[1:]))
        self.assertEqual(fake.calls[1]["args"][fake.calls[1]["args"].index("--effort") + 1], "medium")

    # ---- 6. stable prefix ---------------------------------------------------------------------------------------
    def test_stable_prefix_is_byte_identical_across_runs_kinds_time_and_hash_seeds(self):
        self.configure("claude")
        first = self.engine.chat({"message": "Prepare the launch report", "agent_id": "a-lead", "channel": "dm-a-lead"})
        second = self.engine.enqueue("a-lead", "A peer request", "peer", root_id=first["id"])
        a = self.engine.prompt(self.actor(first), self.store.get("runs", first["id"]))
        with patch("time.time", return_value=1.0), patch("agentflow.store.now", return_value="1999-01-01T00:00:00Z"):
            b = self.engine.prompt(self.actor(second), self.store.get("runs", second["id"]))
        self.assertEqual(a.system.encode(), b.system.encode())
        self.assertEqual(a.system_digest, b.system_digest)
        code = ("from agentflow.prompting import stable_prompt\nactor={'id':'a','name':'N','role':'R','description':'D','instructions':'I'}\n"
                "print(stable_prompt(actor, False, [{'id':'s','name':'n','instructions':'i'}], [{'name':'g','instructions':'x'}])[1])")
        digests = {subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(ROOT)},
                                  capture_output=True, text=True, check=True).stdout.strip() for seed in ("1", "2", "777")}
        self.assertEqual(len(digests), 1, digests)

    def test_usage_record_shape(self):
        self.assertIsNone(usage_record(None))
        self.assertEqual(usage_record({"input_tokens": 1, "cached_input_tokens": 2, "cache_creation_tokens": 3, "output_tokens": 4}),
                         {"input": 1, "cached": 2, "cache_write": 3, "output": 4, "context": 6})

    def test_settings_are_validated(self):
        self.assertEqual(self.service.settings({"session_compact_tokens": 32000, "adaptive_effort": False})["session_compact_tokens"], 32000)
        from agentflow.store import DomainError
        for bad in ({"session_compact_tokens": 15999}, {"session_compact_tokens": 200001}, {"session_compact_tokens": "48000"}, {"adaptive_effort": "yes"}):
            with self.assertRaises(DomainError):
                self.service.settings(bad)


del TokenTests  # only DietTests' own tests run from this module
if __name__ == "__main__":
    unittest.main()
