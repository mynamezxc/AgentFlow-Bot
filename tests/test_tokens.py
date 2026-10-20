import base64
import json
import os
import struct
import time
import unittest
import uuid
import zlib
from pathlib import Path
from unittest.mock import patch

import test_runtime as runtime
from agentflow.contract import CORE_TOOLS, TOOL_HINTS, TOOL_SCHEMAS, TOOLS
from agentflow.prompting import PromptBundle
from agentflow.sessions import NativeSessions


class FakeCli:
    """Stands in for subprocess.Popen of the codex / claude CLIs and records every spawn."""
    def __init__(self, provider, script=(), fail_resume=False):
        self.provider, self.script, self.fail_resume, self.calls = provider, list(script), fail_resume, []
        self.usage = {"input_tokens": 50, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 100, "output_tokens": 20}
        self.threads = {}  # codex thread id -> cumulative usage (turn.completed.usage is thread-cumulative)

    def cumulative(self, thread):
        total = self.threads.setdefault(thread, {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0})
        for key, step in (("input_tokens", 1000), ("cached_input_tokens", 800), ("output_tokens", 50)):
            total[key] += step
        return dict(total)

    def __call__(self, args, **kwargs):
        cli = self

        class Proc:
            returncode = 0
            pid = None

            def communicate(self, stdin=None, timeout=None):
                resumed = ("--resume" in args) if cli.provider == "claude" else ("resume" in args)
                cli.calls.append({"args": list(args), "stdin": stdin, "resumed": resumed})
                if resumed and cli.fail_resume:
                    self.returncode = 1
                    return "", "session not found"
                text = cli.script.pop(0) if cli.script else json.dumps({"reply": "done", "state": "complete"})
                if cli.provider == "claude":
                    sid = args[args.index("--resume") + 1] if resumed else args[args.index("--session-id") + 1] if "--session-id" in args else None
                    lines = [{"type": "system", "subtype": "init", "session_id": sid},
                             {"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": sid,
                              "usage": cli.usage, "modelUsage": {"claude-sonnet-test": {}}}]
                else:
                    tid = args[args.index("resume") + 1] if resumed else str(uuid.uuid4())
                    lines = [{"type": "thread.started", "thread_id": tid},
                             {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
                             {"type": "turn.completed", "usage": cli.cumulative(tid)}]
                return "\n".join(json.dumps(line) for line in lines), ""
        return Proc()


def tool_turn(name="memory.search", **arguments):
    return json.dumps({"reply": "Working", "state": "continue", "progress": "Working", "tool_calls": [{"name": name, "arguments": arguments or {"query": "brief"}}]})


FINAL = json.dumps({"reply": "All done", "state": "complete"})


class TokenTests(unittest.TestCase):
    def setUp(self):
        runtime.RuntimeTests.setUp(self)
        self.profile = self.root / "cli-profile"
        self.profile.mkdir()
        self.home_patch = patch.object(self.providers.accounts, "home", return_value=self.profile)
        self.home_patch.start()

    def tearDown(self):
        self.home_patch.stop()
        runtime.RuntimeTests.tearDown(self)

    # ---- helpers ---------------------------------------------------------------------------------
    def configure(self, provider):
        self.providers.cli[provider] = provider + "-fake"
        fingerprints = {p: self.providers.accounts.check(p, force=True)["fingerprint"] for p in ("codex", "claude")}
        self.providers.models = [
            {"provider": "claude", "id": "sonnet", "reasoning_efforts": ["low", "high"], "account_fingerprint": fingerprints["claude"]},
            {"provider": "claude", "id": "opus", "reasoning_efforts": ["low", "high"], "account_fingerprint": fingerprints["claude"]},
            {"provider": "codex", "id": "gpt-test", "reasoning_efforts": ["low", "high"], "account_fingerprint": fingerprints["codex"]},
            {"provider": "codex", "id": "gpt-other", "reasoning_efforts": [], "account_fingerprint": fingerprints["codex"]}]
        model = {"claude": "sonnet", "codex": "gpt-test"}[provider]
        self.store.settings({"execution_enabled": True})
        self.store.patch("agents", "a-lead", {"provider": provider, "model": model, "binding_origin": "owner"})
        return model

    def busy_project(self, run):
        """Make the project big and busy so the run is in the work tier with a realistic context."""
        project = run["project_id"]
        for n in range(24):
            self.store.create("tasks", {"title": f"Workstream {n} deliverable", "description": "x" * 200, "agent_id": "a-seo", "status": "ready",
                                        "project_id": project, "result": "Findings " * 80, "dependencies": []})
        for n in range(12):
            self.store.create("messages", {"agent_id": "a-seo", "role": "agent", "content": "Teammate update " * 40, "channel": "dm-a-lead",
                                           "project_id": project, "kind": "progress", "audience": "internal"})
        for n in range(20):
            self.store.create("checklist", {"title": f"Step {n}", "note": "evidence " * 20, "status": "doing", "agent_ids": ["a-lead"], "project_id": project, "position": n})

    def work_run(self, provider="claude"):
        self.configure(provider)
        run = self.engine.chat({"message": "Prepare the launch report", "agent_id": "a-lead", "channel": "dm-a-lead"})
        self.busy_project(run)
        return run

    def png(self):
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
        raw = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)) + chunk(
            b"IDAT", zlib.compress(b"\x00" + b"\x00\xff\x00" * 2 + b"\x00" + b"\x00\xff\x00" * 2)) + chunk(b"IEND", b"")
        return self.engine.attachments.upload({"name": "input.png", "data": base64.b64encode(raw).decode()})

    def actor(self, run, **extra):
        agent = self.store.get("agents", run["agent_id"])
        return {**agent, **self.engine.models.effective(agent, run), **extra}

    def run_with(self, provider, script, run=None, **cli):
        run = run or self.work_run(provider)
        fake = FakeCli(provider, script, **cli)
        with patch("agentflow.providers.subprocess.Popen", side_effect=fake):
            self.engine.execute(run["id"])
        return self.store.get("runs", run["id"]), fake

    # ---- stable prefix -----------------------------------------------------------------------------
    def test_stable_prefix_is_identical_across_turns_and_volatile_is_separate(self):
        run = self.work_run("claude")
        first = self.engine.prompt(self.actor(run), self.store.get("runs", run["id"]))
        self.store.patch("tasks", [t for t in self.store.all("tasks") if t["title"].startswith("Workstream")][-1]["id"], {"status": "running"})
        later = self.store.patch("runs", run["id"], {"trace": [{"role": "tool", "content": "[]"}], "resume_reason": "Later"})
        second = self.engine.prompt(self.actor(run), later)
        self.assertEqual(first.system, second.system)
        self.assertEqual(first.system_digest, second.system_digest)
        self.assertNotEqual(first.volatile, second.volatile)
        for text in ("TOOL INDEX", "CORE TOOL SCHEMAS", "runtime_guides", "AGENT IDENTITY", "Working-language policy"):
            self.assertIn(text, first.system)
        for volatile_only in ("Prepare the launch report", "Workstream 3", "Owner reply language"):
            self.assertNotIn(volatile_only, first.system)
            self.assertIn(volatile_only, first.volatile)
        self.assertTrue(str(first).startswith(first.system) and str(first).endswith(first.volatile))
        other = self.engine.prompt(self.actor(run, instructions="Different role instructions"), run)
        self.assertNotEqual(first.system_digest, other.system_digest)

    def test_tool_index_lists_every_tool_and_core_schemas_only(self):
        run = self.work_run("claude")
        system = self.engine.prompt(self.actor(run), run).system
        for name in TOOLS:
            self.assertIn(name, system)
        self.assertFalse(set(TOOLS) - set(TOOL_HINTS))
        section = system.split("=== CORE TOOL SCHEMAS ===\n")[1].split("\n")[0]
        schemas = json.loads(section)
        self.assertEqual(set(schemas), set(CORE_TOOLS))
        self.assertNotIn("browser.act", schemas)
        self.assertIn("tools.inspect", system)

    # ---- progressive disclosure ----------------------------------------------------------------------
    def test_tools_inspect_returns_schemas_and_non_core_errors_carry_schema(self):
        run = self.work_run("claude")
        actor = self.store.get("agents", "a-lead")
        result = self.engine.tool(actor, {"name": "tools.inspect", "arguments": {"names": ["browser.open", "nope"]}}, run)
        self.assertEqual(result["schemas"], {"browser.open": TOOL_SCHEMAS["browser.open"]})
        self.assertEqual(result["unknown"], ["nope"])
        self.assertIn("builtin", self.engine.tool(actor, {"name": "tools.inspect", "arguments": {"query": "snapshot"}}, run))
        bad = self.engine.execute_tool(actor, {"name": "agents.create", "arguments": {}}, run, 0)
        self.assertIn("error", bad)
        self.assertEqual(bad["schema"], TOOL_SCHEMAS["agents.create"])
        core = self.engine.execute_tool(actor, {"name": "workspace.read", "arguments": {"path": "../x"}}, self.store.get("runs", run["id"]), 1)
        self.assertIn("error", core)
        self.assertNotIn("schema", core)

    # ---- Claude sessions -------------------------------------------------------------------------------
    def test_claude_second_turn_resumes_with_a_much_smaller_delta(self):
        run, fake = self.run_with("claude", [tool_turn(query="launch"), FINAL])
        self.assertEqual(len(fake.calls), 2)
        first, second = fake.calls
        self.assertNotIn("--no-session-persistence", first["args"])
        self.assertIn("--session-id", first["args"])
        session_id = first["args"][first["args"].index("--session-id") + 1]
        self.assertEqual(second["args"][second["args"].index("--resume") + 1], session_id)
        self.assertNotIn("--session-id", second["args"])
        same = lambda a: a[a.index("--system-prompt"):a.index("--system-prompt") + 2]
        self.assertEqual(same(first["args"]), same(second["args"]))
        self.assertLess(len(second["stdin"]), 0.3 * len(first["stdin"]), (len(first["stdin"]), len(second["stdin"])))
        self.assertIn("SESSION UPDATE", second["stdin"])
        self.assertIn("memory.search", second["stdin"])
        self.assertNotIn("Workstream 3", second["stdin"])
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["native_session"]["id"], session_id)
        self.assertEqual(run["native_session"]["turns"], 2)
        self.assertIn("tasks", run["native_session"]["context_digests"])
        self.assertEqual(self.providers.sessions.entries()[0]["session_id"], session_id)

    def test_only_changed_sections_and_new_records_are_resent(self):
        run = self.work_run("claude")
        fake = FakeCli("claude", [tool_turn(query="x"), FINAL])
        task = [t for t in self.store.all("tasks") if t["title"].startswith("Workstream")][3]
        original = self.providers.infer

        def infer(agent, prompt, run_id):
            if not fake.calls:
                self.store.patch("tasks", task["id"], {"status": "running"})
            return original(agent, prompt, run_id)
        with patch("agentflow.providers.subprocess.Popen", side_effect=fake), patch.object(self.providers, "infer", side_effect=infer):
            self.engine.execute(run["id"])
        delta = fake.calls[1]["stdin"]
        self.assertIn(task["id"], delta)
        self.assertEqual(delta.count('"id":"t-'), 1)
        self.assertNotIn("CONTEXT checklist", delta)

    def test_model_change_effort_or_digest_invalidates_the_session(self):
        run, _ = self.run_with("claude", [tool_turn(query="x"), tool_turn(query="y"), tool_turn(query="z")][:1] + [FINAL])
        done = self.store.get("runs", run["id"])
        state = done["native_session"]
        actor = self.actor(done)
        self.assertIsNotNone(self.engine.prompt({**actor, "provider": "claude", "model": "sonnet"}, done).delta)
        self.assertEqual(self.engine.prompt({**actor, "provider": "claude", "model": "sonnet", "reasoning_effort": "high"}, done).resume_id, state["id"])  # effort never invalidates
        for change in ({"model": "opus"}, {"provider": "codex", "model": "gpt-test"}, {"instructions": "changed"}):
            bundle = self.engine.prompt({**actor, **change}, done)
            self.assertIsNone(bundle.delta, change)
            self.assertIsNone(bundle.resume_id)
        for field, value in (("turns", 41), ("est_tokens", 140001)):
            stale = {**done, "native_session": {**state, field: value}}
            self.assertIsNone(self.engine.prompt(actor, stale).delta, field)
        self.store.settings({"native_sessions": False})
        self.assertIsNone(self.engine.prompt(actor, done).delta)

    def test_model_switch_mid_run_starts_a_fresh_full_prompt_session(self):
        run = self.work_run("claude")
        calls = []
        fake = FakeCli("claude", [tool_turn("models.switch", provider="claude", model="opus"), FINAL])
        with patch("agentflow.providers.subprocess.Popen", side_effect=fake):
            self.engine.execute(run["id"])
        first, second = fake.calls
        self.assertIn("--session-id", second["args"])
        self.assertNotIn("--resume", second["args"])
        self.assertEqual(second["args"][second["args"].index("--model") + 1], "opus")
        self.assertGreater(len(second["stdin"]), 0.5 * len(first["stdin"]))
        self.assertNotIn("SESSION UPDATE", second["stdin"])

    def test_images_are_sent_only_once_per_session(self):
        self.configure("codex")
        image = self.png()
        run = self.engine.chat({"message": "Look at the picture", "agent_id": "a-lead", "channel": "dm-a-lead", "attachment_ids": [image["id"]]})
        self.busy_project(run)
        run, fake = self.run_with("codex", [tool_turn(query="x"), FINAL], run=run)
        self.assertEqual(fake.calls[0]["args"].count("--image"), 1)
        self.assertEqual(fake.calls[1]["args"].count("--image"), 0)
        self.assertEqual(len(run["native_session"]["sent_images"]), 1)

    # ---- Codex sessions -----------------------------------------------------------------------------------
    def test_codex_resume_arguments_and_content_addressed_instructions(self):
        run, fake = self.run_with("codex", [tool_turn(query="launch"), FINAL])
        first, second = fake.calls
        self.assertNotIn("--ephemeral", first["args"])
        self.assertEqual(first["args"][:2], ["codex-fake", "exec"])
        self.assertEqual(first["args"][-1], "-")
        self.assertNotIn("resume", first["args"])
        thread = run["native_session"]["id"]
        self.assertEqual(second["args"][:2], ["codex-fake", "exec"])
        self.assertEqual(second["args"][-3:], ["resume", thread, "-"])
        self.assertNotIn("--ephemeral", second["args"])
        self.assertLess(len(second["stdin"]), 0.3 * len(first["stdin"]))
        configs = [a for a in second["args"] if a.startswith("model_instructions_file=")]
        self.assertEqual(configs, [a for a in first["args"] if a.startswith("model_instructions_file=")])
        path = Path(json.loads(configs[0].split("=", 1)[1]))
        self.assertEqual(path.parent, self.store.directory / "prompts")
        self.assertEqual(path.name, run["native_session"]["system_digest"] + ".md")
        self.assertIn("CORE TOOL SCHEMAS", path.read_text(encoding="utf-8"))
        self.assertEqual(self.providers.sessions.entries()[0]["session_id"], thread)

    def test_failed_resume_retries_once_with_a_fresh_full_session(self):
        run, fake = self.run_with("codex", [tool_turn(query="x"), FINAL], fail_resume=True)
        self.assertEqual([c["resumed"] for c in fake.calls], [False, True, False])
        fresh_first, failed, fresh_second = fake.calls
        self.assertEqual(run["status"], "done")
        self.assertNotIn("SESSION UPDATE", fresh_second["stdin"])
        self.assertGreater(len(fresh_second["stdin"]), 0.8 * len(fresh_first["stdin"]))
        self.assertIn("YOUR CHECKPOINT", fresh_second["stdin"])
        self.assertEqual(run["native_session"]["turns"], 1)
        self.assertNotEqual(run["native_session"]["id"], failed["args"][failed["args"].index("resume") + 1])

    def test_repeated_resume_failures_disable_native_sessions_for_the_provider(self):
        self.configure("claude")
        fake = FakeCli("claude", fail_resume=True)
        agent = {**self.store.get("agents", "a-lead"), "provider": "claude", "model": "sonnet"}
        run = self.engine.chat({"message": "Prepare the launch report", "agent_id": "a-lead", "channel": "dm-a-lead"})
        self.busy_project(run)
        with patch("agentflow.providers.subprocess.Popen", side_effect=fake):
            for _ in range(5):
                current = self.store.get("runs", run["id"])
                bundle = self.engine.prompt(agent, current)
                self.providers.infer(agent, bundle, run["id"])
                account = self.providers.account_runs.pop(run["id"])
                self.store.patch("runs", run["id"], self.engine.native_patch(current, bundle, account, "x"))
        self.assertIn("claude", self.providers._native_state()["disabled"])
        self.assertNotIn("--session-id", fake.calls[-1]["args"])
        self.assertIn("--no-session-persistence", fake.calls[-1]["args"])

    def test_disabling_native_sessions_keeps_the_stable_split_but_no_session(self):
        self.store.settings({"native_sessions": False})
        run, fake = self.run_with("codex", [tool_turn(query="x"), FINAL])
        self.assertTrue(all("--ephemeral" in c["args"] and "resume" not in c["args"] for c in fake.calls))
        self.assertIsNone(run.get("native_session"))
        self.assertIn("model_instructions_file=", " ".join(fake.calls[0]["args"]))

    # ---- chat tier -------------------------------------------------------------------------------------------
    def test_chat_tier_is_lite_with_low_effort_and_upgrades_after_a_tool_call(self):
        self.configure("codex")
        run = self.engine.chat({"message": "Hello team", "agent_id": "a-lead", "channel": "dm-a-lead"})
        responses = [tool_turn(query="hello"), FINAL]
        with patch.object(self.providers, "infer", side_effect=responses) as inference:
            self.engine.execute(run["id"])
        first, second = (c.args for c in inference.call_args_list)
        self.assertEqual(first[1].tier, "lite")
        self.assertEqual(first[0]["reasoning_effort"], "low")
        self.assertEqual(second[1].tier, "work")
        self.assertIsNone(second[0]["reasoning_effort"])
        self.assertEqual(self.store.get("runs", run["id"])["tier"], "work")
        self.assertEqual(first[1].system, second[1].system)
        for section in ("CONTEXT tasks:", "CONTEXT team:", "CONTEXT available_models:", "CONTEXT checklist:"):
            self.assertNotIn(section, first[1].volatile)
        self.assertIn("team_roster", first[1].stats["sections"])
        self.assertIn("CONTEXT project", second[1].volatile)
        self.assertLess(len(first[1].volatile), 0.5 * len(second[1].volatile))

    def test_chat_tier_stays_work_for_busy_projects_attachments_and_long_messages(self):
        self.configure("codex")
        run = self.engine.chat({"message": "Hello team", "agent_id": "a-lead", "channel": "dm-a-lead"})
        builder = self.engine.builder()
        self.assertTrue(builder.is_lite(run))
        self.assertFalse(builder.is_lite({**run, "prompt": "x" * 801}))
        self.assertFalse(builder.is_lite({**run, "attachment_ids": ["att"]}))
        self.assertFalse(builder.is_lite({**run, "tier": "work"}))
        self.assertFalse(builder.is_lite({**run, "kind": "task"}))
        self.store.create("tasks", {"title": "Open work", "agent_id": "a-seo", "status": "ready", "project_id": run["project_id"]})
        self.assertFalse(builder.is_lite(run))

    def test_lite_effort_requires_unset_effort_and_model_support(self):
        self.configure("codex")
        run = self.engine.chat({"message": "Hi", "agent_id": "a-lead", "channel": "dm-a-lead"})
        bundle = self.engine.prompt(self.actor(run), run)
        self.assertEqual(self.engine.builder().lite_effort(self.actor(run), bundle)["reasoning_effort"], "low")
        self.assertEqual(self.engine.builder().lite_effort(self.actor(run, reasoning_effort="high"), bundle)["reasoning_effort"], "high")
        self.assertIsNone(self.engine.builder().lite_effort(self.actor(run, model="gpt-other"), bundle).get("reasoning_effort"))
        self.store.settings({"chat_tier_low_effort": False})
        self.assertIsNone(self.engine.builder().lite_effort(self.actor(run), bundle).get("reasoning_effort"))

    # ---- accounting --------------------------------------------------------------------------------------------
    def test_usage_is_parsed_for_both_providers_and_accumulated(self):
        run, _ = self.run_with("claude", [tool_turn(query="x"), FINAL])
        self.assertEqual(run["token_usage"], {"input_tokens": 100, "cached_input_tokens": 1800, "cache_creation_tokens": 200, "output_tokens": 40, "turns": 2})
        root = self.store.get("runs", run["root_id"])
        self.assertEqual(root["family_tokens"], 100 + 200 + 40)
        self.assertEqual(root["family_cached_tokens"], 1800)
        self.assertEqual(self.store.get("agents", "a-lead")["token_totals"]["turns"], 2)
        self.configure("codex")
        codex, _ = self.run_with("codex", [tool_turn(query="x"), FINAL], run=self.engine.chat({"message": "Second job", "agent_id": "a-lead", "channel": "dm-a-lead"}))
        self.assertEqual(codex["token_usage"], {"input_tokens": 400, "cached_input_tokens": 1600, "cache_creation_tokens": 0, "output_tokens": 100, "turns": 2})
        self.assertEqual(self.store.get("agents", "a-lead")["token_totals"]["turns"], 4)

    def test_infer_publishes_usage_and_keeps_actual_models(self):
        self.configure("claude")
        agent = {**self.store.get("agents", "a-lead"), "provider": "claude", "model": "sonnet"}
        with patch("agentflow.providers.subprocess.Popen", side_effect=FakeCli("claude", ['{"reply":"x"}'])):
            self.providers.infer(agent, "plain legacy prompt", "legacy-run")
        recorded = self.providers.account_runs["legacy-run"]
        self.assertEqual(recorded["usage"], {"input_tokens": 50, "cached_input_tokens": 900, "cache_creation_tokens": 100, "output_tokens": 20})
        self.assertEqual(recorded["actual_models"], ["claude-sonnet-test"])
        self.assertNotIn("native", recorded)

    # ---- session registry --------------------------------------------------------------------------------------
    def test_prune_deletes_only_registered_finished_sessions(self):
        claude_dir = self.profile / "projects" / "-work"
        codex_dir = self.profile / "sessions" / "2026" / "10" / "19"
        claude_dir.mkdir(parents=True)
        codex_dir.mkdir(parents=True)
        ids = {name: str(uuid.uuid4()) for name in ("claude_old", "claude_kept", "claude_recent", "claude_foreign", "codex_old")}
        files = {"claude_old": claude_dir / (ids["claude_old"] + ".jsonl"), "claude_kept": claude_dir / (ids["claude_kept"] + ".jsonl"),
                 "claude_recent": claude_dir / (ids["claude_recent"] + ".jsonl"), "claude_foreign": claude_dir / (ids["claude_foreign"] + ".jsonl"),
                 "codex_old": codex_dir / ("rollout-2026-10-19T01-02-03-" + ids["codex_old"] + ".jsonl")}
        for file in files.values():
            file.write_text("x" * 100)
        sessions = NativeSessions(self.providers)
        self.assertTrue(sessions.register("run-old", "claude", ids["claude_old"]))
        self.assertTrue(sessions.register("run-live", "claude", ids["claude_kept"]))
        self.assertTrue(sessions.register("run-new", "claude", ids["claude_recent"]))
        self.assertTrue(sessions.register("run-old", "codex", ids["codex_old"]))
        self.assertFalse(sessions.register("run-x", "claude", "../../etc/passwd"))
        entries = sessions._load()
        for entry in entries:
            if entry["run_id"] != "run-new":
                entry["created"] = time.time() - 7200
        sessions._save(entries)
        result = sessions.prune({"run-live"}, older_than_s=3600)
        self.assertEqual(result, {"freed_bytes": 200, "files": 2})
        self.assertFalse(files["claude_old"].exists() or files["codex_old"].exists())
        self.assertTrue(files["claude_kept"].exists() and files["claude_recent"].exists() and files["claude_foreign"].exists())
        self.assertEqual({e["run_id"] for e in sessions.entries()}, {"run-live", "run-new"})
        self.assertIs(self.providers.sessions, self.providers.sessions)

    def test_prompt_files_are_pruned_to_the_newest_fifty(self):
        for number in range(60):
            bundle = PromptBundle("x", system=f"stable {number}", system_digest=f"{number:064x}")
            path = self.providers._prompt_file(bundle)
            os.utime(path, (1000 + number, 1000 + number))
        remaining = sorted(p.name for p in (self.store.directory / "prompts").glob("*.md"))
        self.assertLessEqual(len(remaining), 51)
        self.assertIn(f"{59:064x}.md", remaining)
        self.assertNotIn(f"{0:064x}.md", remaining)

    def test_checkpoint_delta_survives_trace_trimming(self):
        run = self.work_run("claude")
        builder = self.engine.builder()
        trace = [{"role": "tool", "content": "old result"}]
        steps = 0
        state = None
        for n in range(1, 40):
            current = {**self.store.get("runs", run["id"]), "trace": trace[-28:], "steps": steps}
            marker = builder.marker(current)
            trace = trace + [{"role": "assistant", "content": f"answer {n}"}, {"role": "tool", "content": f"result {n}"}]
            steps += 1
            later = {**current, "trace": trace[-28:], "steps": steps}
            delta = builder.new_results(later, marker)
            self.assertEqual([e["content"] for e in delta], [f"result {n}"], n)
        self.assertIsNone(builder.new_results(later, {"steps": 5, "tail": 0}))  # that assistant entry was trimmed away


if __name__ == "__main__":
    unittest.main()
