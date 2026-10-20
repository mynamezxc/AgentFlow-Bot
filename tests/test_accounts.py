import json
import os
import threading
import time
import unittest
from unittest.mock import Mock, patch

import test_runtime as runtime
from agentflow.accounts import AccountChanged, AccountUnavailable, ProviderLimit
from agentflow.providers import Providers


class AccountTests(unittest.TestCase):
    def setUp(self):
        runtime.RuntimeTests.setUp(self)
        self.profile = self.root / "cli-profile"
        self.profile.mkdir()
        self.home_patch = patch.object(self.providers.accounts, "home", return_value=self.profile)
        self.home_patch.start()
        self.refresh_patch = patch.object(self.providers, "refresh_models", return_value=[])
        self.refresh_patch.start()

    def tearDown(self):
        for thread in self.providers.status_threads.values():
            thread.join(timeout=5)
        self.home_patch.stop()
        self.refresh_patch.stop()
        runtime.RuntimeTests.tearDown(self)

    def account(self, email="first@example.test"):
        return {"type": "chatgpt", "email": email, "planType": "pro"}

    def test_account_switch_discards_old_catalog_and_verification(self):
        with patch.object(self.providers, "read_account", return_value=self.account()):
            first = self.providers.accounts.check("codex", force=True)
        self.providers.models = [{"id": "old-model", "provider": "codex", "account_fingerprint": first["fingerprint"]}]
        self.providers.verified.add("codex:old-model")
        with patch.object(self.providers, "read_account", return_value=self.account("second@example.test")):
            second = self.providers.accounts.check("codex", force=True)
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(self.providers.models, [])
        self.assertNotIn("codex:old-model", self.providers.verified)
        self.assertEqual(second["label"], "second@example.test")

    def test_token_refresh_same_account_keeps_identity_and_contains_no_tokens(self):
        auth = self.profile / "auth.json"
        auth.write_text(json.dumps({"tokens": {"account_id": "scope-a", "access_token": "old-secret"}}))
        with patch.object(self.providers, "read_account", return_value=self.account()):
            first = self.providers.accounts.check("codex", force=True)
            self.providers.verified.add("codex:observed")
            auth.write_text(json.dumps({"tokens": {"account_id": "scope-a", "access_token": "refreshed-secret"}}))
            second = self.providers.accounts.check("codex", force=True)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertIn("codex:observed", self.providers.verified)
        state = self.providers.accounts.path.read_text()
        for forbidden in ("old-secret", "refreshed-secret", "scope-a"):
            self.assertNotIn(forbidden, state)

    def test_same_email_different_workspace_invalidates_identity(self):
        auth = self.profile / "auth.json"
        with patch.object(self.providers, "read_account", return_value=self.account()):
            auth.write_text(json.dumps({"tokens": {"account_id": "workspace-a"}}))
            first = self.providers.accounts.check("codex", force=True)
            auth.write_text(json.dumps({"tokens": {"account_id": "workspace-b"}}))
            second = self.providers.accounts.check("codex", force=True)
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])

    def test_model_config_and_token_refresh_do_not_hide_same_account_usage(self):
        auth=self.profile/'auth.json'
        auth.write_text(json.dumps({'tokens':{'account_id':'workspace-a','access_token':'old'}}))
        with patch.object(self.providers,'read_account',return_value=self.account()):
            state=self.providers.accounts.check('codex',force=True)
            self.providers.usage.save('codex',state['fingerprint'],[{'id':'codex:primary','used_percent':23}],'test')
            revision=self.providers.accounts.revision('codex')
            for i in range(4):
                (self.profile/'config.toml').write_text(f'model="model-{i}"\nmodel_reasoning_effort="high"')
                auth.write_text(json.dumps({'tokens':{'account_id':'workspace-a','access_token':f'refresh-{i}'}}))
                self.assertEqual(self.providers.accounts.revision('codex'),revision)
                self.assertEqual(self.providers.usage.public('codex',pending=True)['windows'][0]['used_percent'],23)
            auth.write_text(json.dumps({'tokens':{'account_id':'workspace-b','access_token':'new-account'}}))
            self.assertEqual(self.providers.usage.public('codex',pending=True)['windows'],[])

    def test_codex_restart_reuses_only_matching_confirmed_identity(self):
        auth=self.profile/'auth.json'
        auth.write_text(json.dumps({'tokens':{'account_id':'workspace-a'}}))
        with patch.object(self.providers,'read_account',return_value=self.account()):
            state=self.providers.accounts.check('codex',force=True)
        from agentflow.accounts import Accounts
        with patch.object(Accounts,'home',return_value=self.profile):
            restored=Accounts(self.providers)
            self.assertEqual(restored.current['codex']['fingerprint'],state['fingerprint'])
            auth.write_text(json.dumps({'tokens':{'account_id':'workspace-b'}}))
            switched=Accounts(self.providers)
            self.assertNotIn('codex',switched.current)

    def test_codex_metadata_cleanup_never_blocks_on_reader_owned_stdout(self):
        release=threading.Event();closed=threading.Event()
        class HeldPipe:
            def __iter__(self):
                yield json.dumps({'id':1,'result':{}})+'\n'
                yield json.dumps({'id':2,'result':{'account':{'type':'chatgpt'}}})+'\n'
                yield json.dumps({'id':3,'result':{'rateLimits':{'primary':{'usedPercent':25}}}})+'\n'
                release.wait(timeout=5)
            def close(self):closed.set()
        process=Mock(pid=None);process.stdout=HeldPipe();process.poll.return_value=None
        self.providers.cli['codex']='metadata-test.exe'
        try:
            with patch('agentflow.providers.subprocess.Popen',return_value=process):
                started=time.monotonic()
                result=self.providers._codex_models(include_models=False,read_usage=True)
                self.assertLess(time.monotonic()-started,2)
            self.assertEqual(result['limits']['rateLimits']['primary']['usedPercent'],25)
            self.assertFalse(closed.is_set())
            process.kill.assert_called_once()
        finally:
            release.set()
            self.assertTrue(closed.wait(timeout=2))

    def test_inference_uses_current_native_login_and_preserves_auth_store(self):
        (self.profile / "config.toml").write_text('cli_auth_credentials_store="keyring"\nforced_login_method="chatgpt"')
        self.providers.cli["codex"] = "codex-test.exe"
        process = Mock(returncode=0)
        process.communicate.return_value = (json.dumps({"type": "item.completed",
            "item": {"type": "agent_message", "text": '{"reply":"Current account"}'}}), "")
        with patch.object(self.providers, "read_account", return_value=self.account()), \
             patch.object(self.providers, "_codex_models", return_value=[{"id": "observed", "provider": "codex"}]), \
             patch.object(self.providers, "refresh_models", wraps=Providers.refresh_models.__get__(self.providers)), \
             patch("agentflow.providers.subprocess.Popen", return_value=process) as launch, \
             patch.dict(os.environ, {"CODEX_API_KEY": "stale-api-override", "OPENAI_API_KEY": "stale-openai"}):
            result = self.providers.infer({"provider": "codex", "model": "observed", "name": "Worker"}, "Test", "run-test")
        self.assertEqual(result, '{"reply":"Current account"}')
        argv = launch.call_args.args[0]
        self.assertIn('cli_auth_credentials_store="keyring"', argv)
        self.assertIn('forced_login_method="chatgpt"', argv)
        self.assertNotIn("CODEX_API_KEY", launch.call_args.kwargs["env"])
        self.assertNotIn("OPENAI_API_KEY", launch.call_args.kwargs["env"])
        self.assertIn("codex:observed", self.providers.verified)
        self.assertEqual(self.providers.active_accounts, {})

    def test_switch_during_inference_discards_old_result(self):
        active = {"account": self.account()}
        self.providers.cli["codex"] = "codex-test.exe"
        process = Mock(returncode=0)
        def communicate(*args, **kwargs):
            active["account"] = self.account("second@example.test")
            return json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": '{"reply":"Old account result"}'}}), ""
        process.communicate.side_effect = communicate
        with patch.object(self.providers, "read_account", side_effect=lambda provider: active["account"]), \
             patch.object(self.providers, "_codex_models", return_value=[{"id": "observed", "provider": "codex"}]), \
             patch.object(self.providers, "refresh_models", wraps=Providers.refresh_models.__get__(self.providers)), \
             patch("agentflow.providers.subprocess.Popen", return_value=process):
            with self.assertRaises(AccountChanged):
                self.providers.infer({"provider": "codex", "model": "observed", "name": "Worker"}, "Test", "run-test")
        self.assertNotIn("codex:observed", self.providers.verified)
        self.assertEqual(self.providers.active_accounts, {})

    def test_detected_switch_cancels_an_old_active_process(self):
        with patch.object(self.providers, "read_account", return_value=self.account()):
            first = self.providers.accounts.check("codex", force=True)
        process = Mock()
        self.providers.processes["active-run"] = process
        self.providers.active_accounts["active-run"] = {"provider": "codex", "fingerprint": first["fingerprint"]}
        with patch.object(self.providers, "read_account", return_value=self.account("second@example.test")):
            self.providers.accounts.check("codex", force=True)
        process.kill.assert_called_once()
        self.providers.processes.clear()

    def test_logout_waits_and_login_resumes_checkpoint(self):
        actor = self.store.patch("agents", "a-code", {"provider": "codex", "model": "observed"})
        run = self.engine.enqueue(actor["id"], "Read project")
        with patch.object(self.providers, "read_account", return_value=None):
            self.engine.execute(run["id"])
        self.assertEqual(self.store.get("runs", run["id"])["status"], "blocked_account")
        with patch.object(self.providers, "read_account", return_value=self.account()):
            self.providers.accounts.check("codex", force=True)
            self.engine.wake_waiters()
        self.assertEqual(self.store.get("runs", run["id"])["status"], "queued")

    def test_saved_gateway_key_takes_precedence_and_invalidates_old_session(self):
        self.store.settings({"gateway_url": "http://localhost:20128/v1"})
        with patch.dict(os.environ, {"GATEWAY_API_KEY": "old-env-key"}):
            first = self.providers.accounts.check("gateway", force=True)
            self.providers.verified.add("gateway:old-model")
            self.vault.set({"GATEWAY_API_KEY": "new-owner-key"})
            self.assertEqual(self.vault.get("GATEWAY_API_KEY"), "new-owner-key")
            second = self.providers.accounts.check("gateway", force=True)
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotIn("gateway:old-model", self.providers.verified)

    def test_claude_account_change_invalidates_old_catalog_and_inference(self):
        account = {"type": "claude.ai", "email": "current@example.test", "org_id": "team-a"}
        with patch.object(self.providers, "read_account", return_value=account):
            first = self.providers.accounts.check("claude", force=True)
        self.providers.models = [{"id": "opus", "provider": "claude", "account_fingerprint": first["fingerprint"]}]
        self.providers.verified.add("claude:opus")
        with patch.object(self.providers, "read_account", return_value={**account, "org_id": "team-b"}):
            second = self.providers.accounts.check("claude", force=True)
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(self.providers.models, [])
        self.assertNotIn("claude:opus", self.providers.verified)

    def test_claude_inference_keeps_native_auth_and_records_actual_model(self):
        self.providers.cli["claude"] = "claude-test.exe"
        process = Mock(returncode=0)
        process.communicate.return_value = (json.dumps({"result": '{"reply":"Ready"}',
            "modelUsage": {"claude-opus-test": {"inputTokens": 10}}}), "")
        with patch.object(self.providers, "read_account", return_value={"type": "claude.ai", "email": "current@example.test"}), \
             patch("agentflow.providers.claude_catalog", return_value=[{"value": "opus", "resolvedModel": "claude-opus-test"}]), \
             patch.object(self.providers, "refresh_models", wraps=Providers.refresh_models.__get__(self.providers)), \
             patch("agentflow.providers.subprocess.Popen", return_value=process) as launch:
            result = self.providers.infer({"provider": "claude", "model": "opus", "name": "Planner"}, "Plan", "claude-run")
            process.returncode = 1
            process.communicate.return_value = (json.dumps({"is_error": True, "api_error_status": 429,
                "result": "You've hit your session limit · resets 10:30am (Asia/Ho_Chi_Minh)"}), "")
            with self.assertRaises(ProviderLimit):
                self.providers.infer({"provider": "claude", "model": "opus", "name": "Planner"}, "Plan", "claude-quota")
        self.assertEqual(result, '{"reply":"Ready"}')
        self.assertEqual(self.providers.account_runs["claude-run"]["actual_models"], ["claude-opus-test"])
        args = launch.call_args.args[0]
        self.assertIn("--safe-mode", args)
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", args)

    def test_legacy_history_cannot_verify_a_new_account(self):
        self.store.create("runs", {"agent_id": "a-lead", "status": "done", "model": "legacy-model", "provider": "codex"})
        reopened = Providers(self.store, self.vault, self.engine.workspace)
        self.assertNotIn("codex:legacy-model", reopened.verified)

    def test_probe_failure_does_not_display_previous_account_as_current(self):
        with patch.object(self.providers, "read_account", return_value=self.account()):
            self.providers.accounts.check("codex", force=True)
        self.providers.accounts.checked["codex"] = 0
        self.providers.verified.add("codex:observed")
        self.providers.cli["codex"] = "codex-test.exe"
        with patch.object(self.providers, "read_account", side_effect=runtime.DomainError("Unavailable", 502)):
            self.providers.status()
            for thread in self.providers.status_threads.values():
                thread.join(timeout=5)
            status = self.providers.status()
        codex = next(p for p in status if p["id"] == "codex")
        self.assertFalse(codex["verified"])
        self.assertNotEqual(codex["account_label"], "first@example.test")

    def test_slow_account_probe_never_blocks_workspace_state(self):
        self.providers.cli = {"codex": "codex-test.exe"}
        release = threading.Event()
        entered = threading.Event()
        def slow(provider):
            entered.set()
            release.wait(timeout=5)
            return self.account()
        with patch.object(self.providers, "read_account", side_effect=slow):
            try:
                start = time.monotonic()
                status = self.providers.status()
                self.assertLess(time.monotonic() - start, 0.5)
                self.assertTrue(entered.wait(timeout=1))
                codex = next(p for p in status if p["id"] == "codex")
                self.assertFalse(codex["verified"])
                self.assertIn("Checking current account", codex["account_label"])
            finally:
                release.set()
                for thread in self.providers.status_threads.values():
                    thread.join(timeout=5)
        status = self.providers.status()
        self.assertEqual(next(p for p in status if p["id"] == "codex")["account_label"], "first@example.test")
