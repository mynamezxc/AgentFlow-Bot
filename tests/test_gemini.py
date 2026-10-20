import base64
import http.client
import json
import os
import stat
import struct
import sys
import threading
import zlib
import time
import unittest
import unittest.mock
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import test_runtime as runtime
from agentflow.accounts import Accounts, ProviderLimit
from agentflow.native import discover_cli, gemini_acp_catalog, launch_args, runnable_cli
from agentflow.prompting import PromptBundle, image_key
from agentflow.providers import Providers, gemini_retry_seconds
from agentflow.quota import TransientLimit
from agentflow.store import DomainError
from server import make_handler

FAKE_GEMINI = r'''
import json, os, sys, time, uuid
base = os.environ["FAKE_GEMINI_DIR"]
scenario = json.load(open(os.path.join(base, "scenario.json")))
args = sys.argv[1:]
def log(entry):
    with open(os.path.join(base, "calls.jsonl"), "a") as handle:
        handle.write(json.dumps(entry) + "\n")
if "--experimental-acp" in args:
    mode = scenario.get("acp", {})
    log({"kind": "acp", "api_key": os.environ.get("GEMINI_API_KEY"), "cwd": os.getcwd()})
    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        log({"kind": "rpc", "method": method, "params": message.get("params")})
        if mode.get("hang"):
            time.sleep(60)
        def reply(result=None, error=None):
            body = {"jsonrpc": "2.0", "id": message["id"]}
            body["error" if error else "result"] = error or result
            sys.stdout.write(json.dumps(body) + "\n")
            sys.stdout.flush()
        if method == "initialize":
            reply({"protocolVersion": 1, "authMethods": [{"id": "oauth-personal"}, {"id": "gemini-api-key"}]})
        elif method == "authenticate":
            reply({})
        elif method == "session/new":
            if mode.get("error"):
                reply(error={"code": -32000, "message": "Authentication required"})
            elif mode.get("exit"):
                sys.exit(3)
            else:
                result = {"sessionId": "acp-1"}
                if mode.get("models") is not None:
                    result["models"] = {"availableModels": mode["models"], "currentModelId": mode["models"][0]["modelId"]}
                reply(result)
    sys.exit(0)
stdin = sys.stdin.read()
runs = [json.loads(l) for l in open(os.path.join(base, "calls.jsonl")) if json.loads(l).get("kind") == "run"] if os.path.exists(os.path.join(base, "calls.jsonl")) else []
turns = scenario["turns"]
turn = turns[min(len(runs), len(turns) - 1)]
resumed = "-r" in args
sid = args[args.index("-r") + 1] if resumed else str(uuid.uuid4())
system_md = os.environ.get("GEMINI_SYSTEM_MD")
log({"kind": "run", "args": args, "stdin": stdin, "system_md": system_md,
     "system_text": open(system_md, encoding="utf-8").read() if system_md and os.path.isfile(system_md) else None,
     "api_key": os.environ.get("GEMINI_API_KEY"), "auth_type": os.environ.get("GEMINI_DEFAULT_AUTH_TYPE"), "cwd": os.getcwd()})
if turn.get("fail_resume") and resumed:
    sys.stderr.write("Error resuming session: session not found")
    sys.exit(1)
if "raw" in turn:
    print(turn["raw"])
    if turn.get("stderr"):
        sys.stderr.write(turn["stderr"])
    sys.exit(turn.get("exit", 0))
if turn.get("events") is not None:
    events = json.loads(json.dumps(turn["events"]).replace("$SID", sid))
else:
    model = args[args.index("-m") + 1]
    events = [{"type": "init", "session_id": sid, "model": model}, {"type": "message", "role": "user", "content": "prompt"}]
    for chunk in turn.get("chunks", [turn.get("text", "ok")]):
        events.append({"type": "message", "role": "assistant", "content": chunk, "delta": True})
    events.append({"type": "result", "status": "success", "stats": turn.get("stats", {
        "total_tokens": 1050, "input_tokens": 1000, "output_tokens": 50, "cached": 800, "input": 200, "duration_ms": 5, "tool_calls": 0,
        "models": {model: {"total_tokens": 1050, "input_tokens": 1000, "output_tokens": 50, "cached": 800, "input": 200}}})})
for event in events:
    print(json.dumps(event), flush=True)
if turn.get("stderr"):
    sys.stderr.write(turn["stderr"])
sys.exit(turn.get("exit", 0))
'''

ACP_MODELS = [{"modelId": "gemini-test-pro", "name": "Gemini Test Pro", "description": "Reasoning"},
              {"modelId": "gemini-test-flash", "name": "Gemini Test Flash", "description": "Fast"}]


def jwt(claims):
    part = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return part({"alg": "none"}) + "." + part(claims) + ".sig"


class GeminiBase(unittest.TestCase):
    def setUp(self):
        runtime.RuntimeTests.setUp(self)
        self.profile = self.root / "gemini-home" / ".gemini"
        self.profile.mkdir(parents=True)
        self.fake = self.root / "fake-gemini"
        self.fake.mkdir()
        script = self.fake / "fake_gemini.py"
        script.write_text(FAKE_GEMINI, encoding="utf-8")
        if os.name == "nt":
            executable = self.fake / "gemini.cmd"
            executable.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
        else:
            executable = self.fake / "gemini"
            executable.write_text(f"#!{sys.executable}\n" + FAKE_GEMINI, encoding="utf-8")
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        self.executable = str(executable)
        self.env = patch.dict(os.environ, {"FAKE_GEMINI_DIR": str(self.fake)})
        self.env.start()
        for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT", "GEMINI_CLI_HOME"):
            os.environ.pop(name, None)
        real = Providers.read_account
        self.account_patch2 = patch.object(self.providers, "read_account",
            side_effect=lambda provider: real(self.providers, provider) if provider == "gemini" else {"type": "test", "label": "Unit account"})
        self.account_patch2.start()
        self.home_patch = patch.object(self.providers.accounts, "home", return_value=self.profile)
        self.home_patch.start()
        self.providers.cli["gemini"] = self.executable
        self.scenario(turns=[{}])

    def tearDown(self):
        for thread in list(self.providers.status_threads.values()):
            thread.join(timeout=5)
        self.home_patch.stop()
        self.account_patch2.stop()
        self.env.stop()
        runtime.RuntimeTests.tearDown(self)

    # ---- helpers -----------------------------------------------------------------------------
    def scenario(self, turns=None, acp=None):
        (self.fake / "scenario.json").write_text(json.dumps({"turns": turns or [{}], "acp": acp if acp is not None else {"models": ACP_MODELS}}))
        (self.fake / "calls.jsonl").unlink(missing_ok=True)

    def log(self, kind):
        path = self.fake / "calls.jsonl"
        entries = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [e for e in entries if e["kind"] == kind]

    def sign_in(self, email="owner@example.test", access="access-secret-1", refresh="refresh-secret-1", claims=None):
        if email:
            (self.profile / "google_accounts.json").write_text(json.dumps({"active": email, "old": []}))
        (self.profile / "oauth_creds.json").write_text(json.dumps({
            "access_token": access, "refresh_token": refresh, "expiry_date": 1, "token_type": "Bearer",
            **({"id_token": jwt(claims)} if claims else {})}))

    def catalog(self):
        self.sign_in()
        self.providers.refresh_models("gemini")

    def actor(self, **extra):
        return {"provider": "gemini", "model": "gemini-test-pro", "name": "Worker", **extra}

    def bundle(self, volatile="VOLATILE", delta=None, resume_id=None, system="SYSTEM PROMPT", sent=()):
        return PromptBundle(system + "\n" + volatile, system=system, system_digest="d" * 16, volatile=volatile, delta=delta,
                            resume_id=resume_id, native=True, provider="gemini", model="gemini-test-pro", sent_images=tuple(sent))


class IdentityAndStatusTests(GeminiBase):
    def test_oauth_identity_comes_from_google_accounts_and_never_stores_tokens(self):
        self.sign_in()
        state = self.providers.accounts.check("gemini", force=True)
        self.assertTrue(state["signed_in"])
        self.assertEqual((state["label"], state["type"]), ("owner@example.test", "oauth"))
        stored = self.providers.accounts.path.read_text()
        for secret in ("access-secret-1", "refresh-secret-1"):
            self.assertNotIn(secret, stored)
        self.assertFalse(self.providers.accounts.check("gemini", force=True)["fingerprint"] != state["fingerprint"])

    def test_credential_refresh_does_not_flap_status_but_account_switch_does(self):
        self.sign_in()
        self.providers.accounts.check("gemini", force=True)
        self.providers.verified.add("gemini:gemini-test-pro")
        first, revision = self.providers.accounts.current["gemini"]["fingerprint"], self.providers.accounts.revision("gemini")
        marks = self.providers.accounts.marks("gemini")
        self.sign_in(access="rotated-access", refresh="refresh-secret-1")  # the CLI rewrites oauth_creds.json on every refresh
        self.assertNotEqual(self.providers.accounts.marks("gemini"), marks)  # a hint only
        self.assertEqual(self.providers.accounts.revision("gemini"), revision)
        self.assertEqual(self.providers.accounts.check("gemini", force=True)["fingerprint"], first)
        row = next(r for r in self.providers._status_rows() if r["id"] == "gemini")
        self.assertTrue(row["connected"])
        self.assertIn("gemini:gemini-test-pro", self.providers.verified)
        self.sign_in(email="other@example.test")
        self.assertNotEqual(self.providers.accounts.revision("gemini"), revision)
        self.assertNotEqual(self.providers.accounts.check("gemini", force=True)["fingerprint"], first)
        self.assertNotIn("gemini:gemini-test-pro", self.providers.verified)

    def test_identity_without_email_uses_id_token_subject_or_refresh_digest(self):
        self.sign_in(email=None, claims={"sub": "subject-1"})
        a = self.providers.accounts.check("gemini", force=True)
        self.assertEqual(a["label"], "Google account (signed in)")
        self.sign_in(email=None, access="new-access", claims={"sub": "subject-1"})
        self.assertEqual(self.providers.accounts.check("gemini", force=True)["fingerprint"], a["fingerprint"])
        self.sign_in(email=None, claims={"sub": "subject-2"})
        self.assertNotEqual(self.providers.accounts.check("gemini", force=True)["fingerprint"], a["fingerprint"])
        self.sign_in(email=None, refresh="digest-a")
        b = self.providers.accounts.check("gemini", force=True)
        self.sign_in(email=None, access="x", refresh="digest-a")
        self.assertEqual(self.providers.accounts.check("gemini", force=True)["fingerprint"], b["fingerprint"])
        self.assertNotIn("digest-a", self.providers.accounts.path.read_text())

    def test_signed_out_then_api_key_then_vertex_modes(self):
        self.assertFalse(self.providers.accounts.check("gemini", force=True)["signed_in"])
        row = next(r for r in self.providers._status_rows() if r["id"] == "gemini")
        self.assertEqual((row["auth"], row["api_key_set"], row["models_source"]), (None, False, None))
        self.vault.set({"GEMINI_API_KEY": "AIza-test-key-123456"})
        state = self.providers.accounts.check("gemini", force=True)
        self.assertEqual((state["signed_in"], state["type"], state["label"]), (True, "api_key", "Gemini API key"))
        self.assertNotIn("AIza-test-key-123456", self.providers.accounts.path.read_text())
        row = next(r for r in self.providers._status_rows() if r["id"] == "gemini")
        self.assertEqual((row["auth"], row["api_key_set"], row["connected"]), ("api_key", True, True))
        self.assertNotIn("AIza-test-key-123456", json.dumps(row))
        with patch.dict(os.environ, {"GOOGLE_GENAI_USE_VERTEXAI": "true", "GOOGLE_CLOUD_PROJECT": "demo-project"}):
            self.vault.set({}, replace_prefix="GEMINI_API_KEY")
            state = self.providers.accounts.check("gemini", force=True)
            self.assertEqual((state["type"], state["label"]), ("vertex", "Vertex AI · demo-project"))

    def test_row_contract_after_probe(self):
        self.sign_in()
        self.providers._probe_status("gemini")
        row = next(r for r in self.providers._status_rows() if r["id"] == "gemini")
        self.assertEqual([row[k] for k in ("id", "installed", "configured", "connected", "account_label", "auth", "api_key_set", "models_source")],
                         ["gemini", True, True, True, "owner@example.test", "oauth", False, "acp"])
        self.assertEqual(row["error"], "")
        self.assertNotIn("disabled", row)

    def test_gemini_cli_home_replaces_the_home_directory(self):
        with patch.dict(os.environ, {"GEMINI_CLI_HOME": str(self.root / "custom")}):
            self.assertEqual(Accounts.home(self.providers.accounts, "gemini"), self.root / "custom" / ".gemini")

    def test_environment_only_carries_the_key_in_api_key_mode(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "env-key-123456", "GOOGLE_API_KEY": "google-key-123456"}):
            self.sign_in()
            self.assertNotIn("GEMINI_API_KEY", self.providers.child_environment())
            self.assertNotIn("GEMINI_API_KEY", self.providers.gemini_environment())  # oauth wins over a stray env key
            os.environ.pop("GEMINI_API_KEY")
            self.vault.set({"GEMINI_API_KEY": "vault-key-123456"})
            self.assertNotIn("GEMINI_API_KEY", self.providers.child_environment())
            env = self.providers.gemini_environment()
            self.assertEqual(env["GEMINI_API_KEY"], "vault-key-123456")
            self.assertNotIn("GOOGLE_API_KEY", env)


class DiscoveryTests(unittest.TestCase):
    def test_windows_npm_shims_prefer_cmd_and_run_through_cmd_exe(self):
        with __import__("tempfile").TemporaryDirectory() as temp:
            npm = Path(temp) / "npm"
            npm.mkdir()
            for name in ("gemini.cmd", "gemini.ps1"):
                (npm / name).write_text("")
            with patch.dict(os.environ, {"APPDATA": temp, "USERPROFILE": temp, "GEMINI_PATH": ""}), patch("agentflow.native.shutil.which", return_value=None):
                os.environ.pop("GEMINI_PATH")
                self.assertEqual(discover_cli("gemini"), str(npm / "gemini.cmd"))
            with patch("agentflow.native.shutil.which", return_value=None), patch.dict(os.environ, {"GEMINI_PATH": str(npm / "gemini.ps1")}):
                self.assertEqual(discover_cli("gemini"), str(npm / "gemini.cmd"))
            (npm / "gemini.cmd").unlink()
            self.assertEqual(runnable_cli(str(npm / "gemini.ps1")), str(npm / "gemini.ps1"))
        line = launch_args(r"C:\Users\Owner Name\AppData\Roaming\npm\gemini.cmd", ["-p", "", "-m", "pro", "--policy", r"C:\data dir\p.toml"], windows=True)
        self.assertEqual(line, 'cmd.exe /d /s /c ""C:\\Users\\Owner Name\\AppData\\Roaming\\npm\\gemini.cmd" -p "" -m pro --policy "C:\\data dir\\p.toml""')
        self.assertEqual(launch_args(r"C:\x\gemini.exe", ["-m", "pro"], windows=True), [r"C:\x\gemini.exe", "-m", "pro"])
        self.assertEqual(launch_args("/usr/bin/gemini", ["-m", "pro"], windows=False), ["/usr/bin/gemini", "-m", "pro"])
        self.assertEqual(launch_args(r"C:\x\gemini.ps1", ["-m", "pro"], windows=True)[:6],
                         ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", r"C:\x\gemini.ps1"])
        with self.assertRaises(DomainError):  # cmd would expand %VAR% inside quotes
            launch_args(r"C:\x\gemini.cmd", ["--policy", "C:\\%TEMP%\\p.toml"], windows=True)


class CatalogTests(GeminiBase):
    def test_acp_is_the_first_source_and_is_asked_correctly(self):
        self.sign_in()
        found = self.providers.refresh_models("gemini")
        self.assertEqual([m["id"] for m in found], ["gemini-test-pro", "gemini-test-flash"])
        first = found[0]
        self.assertEqual((first["provider"], first["source_kind"], first["capabilities"], first["reasoning_efforts"], first["name"]),
                         ("gemini", "acp", ["chat"], [], "Gemini Test Pro"))
        self.assertEqual(first["source"], "Gemini CLI ACP session/new")
        self.assertEqual(first["account_fingerprint"], self.providers.accounts.current["gemini"]["fingerprint"])
        rpc = {e["method"]: e["params"] for e in self.log("rpc")}
        self.assertEqual(rpc["session/new"], {"cwd": str(self.providers.workspace), "mcpServers": []})
        self.assertEqual(rpc["authenticate"], {"methodId": "oauth-personal"})
        self.assertEqual(rpc["initialize"]["protocolVersion"], 1)
        self.assertEqual(self.log("acp")[0]["cwd"], str(self.providers.workspace))
        self.assertEqual([m["id"] for m in self.engine.models.catalog() if m["provider"] == "gemini"], ["gemini-test-pro", "gemini-test-flash"])
        self.assertTrue(json.loads(self.providers.catalog_path.read_text())[0]["source_kind"] in ("acp",))

    def test_api_list_when_acp_has_no_models_with_pagination_and_image_models(self):
        self.vault.set({"GEMINI_API_KEY": "AIza-api-key-123456"})
        self.scenario(acp={"models": None})
        pages = [
            {"models": [{"name": "models/gemini-3-pro", "displayName": "Gemini 3 Pro", "supportedGenerationMethods": ["generateContent", "countTokens"]},
                        {"name": "models/text-embedding-9", "supportedGenerationMethods": ["embedContent"]},
                        {"name": "models/gemini-3-flash-image", "displayName": "Flash Image", "supportedGenerationMethods": ["generateContent"]},
                        {"name": "models/imagen-4.0-generate", "supportedGenerationMethods": ["predict"]}],
             "nextPageToken": "page-2"},
            {"models": [{"name": "models/gemini-3-flash", "supportedGenerationMethods": ["generateContent"]},
                        {"name": "models/gemini-3-flash-tts", "supportedGenerationMethods": ["generateContent"]},
                        {"name": "models/veo-3", "supportedGenerationMethods": ["generateContent"]},
                        {"name": "models/aqa", "supportedGenerationMethods": ["generateAnswer"]}]}]
        with patch("agentflow.providers.http_json", side_effect=pages) as http:
            found = self.providers.refresh_models("gemini")
        self.assertEqual([(m["id"], m["capabilities"], m["source_kind"]) for m in found],
                         [("gemini-3-pro", ["chat"], "api"), ("gemini-3-flash", ["chat"], "api"), ("gemini-3-flash-image", ["image"], "api")])
        self.assertEqual(http.call_args_list[0].kwargs["headers"], {"x-goog-api-key": "AIza-api-key-123456"})
        self.assertTrue(http.call_args_list[0].args[0].startswith("https://generativelanguage.googleapis.com/v1beta/models"))
        self.assertIn("pageToken=page-2", http.call_args_list[1].args[0])
        self.assertEqual([m["id"] for m in self.engine.models.catalog() if m["provider"] == "gemini"], ["gemini-3-pro", "gemini-3-flash"])
        self.assertEqual([m["id"] for m in self.engine.models.catalog("image")], ["gemini-3-flash-image"])
        row = next(r for r in self.providers._status_rows() if r["id"] == "gemini")
        self.assertEqual((row["models_source"], row["auth"]), ("api", "api_key"))
        self.assertEqual(self.log("acp")[0]["api_key"], "AIza-api-key-123456")  # passed explicitly, only to gemini

    def test_acp_chat_models_are_kept_and_api_adds_image_models(self):
        self.vault.set({"GEMINI_API_KEY": "AIza-api-key-123456"})
        page = {"models": [{"name": "models/gemini-3-flash-image", "supportedGenerationMethods": ["generateContent"]},
                           {"name": "models/gemini-api-only", "supportedGenerationMethods": ["generateContent"]}]}
        with patch("agentflow.providers.http_json", return_value=page):
            found = self.providers.refresh_models("gemini")
        self.assertEqual([(m["id"], m["source_kind"], m["capabilities"][0]) for m in found],
                         [("gemini-test-pro", "acp", "chat"), ("gemini-test-flash", "acp", "chat"), ("gemini-3-flash-image", "api", "image")])

    def test_documented_aliases_are_the_last_resort_and_labelled(self):
        self.sign_in()
        for acp in ({"error": True}, {"models": None}, {"exit": True}):
            with self.subTest(acp=acp):
                self.scenario(acp=acp)
                found = self.providers.refresh_models("gemini")
                self.assertEqual([m["id"] for m in found], ["auto", "pro", "flash", "flash-lite"])
                self.assertTrue(all(m["source"] == "Gemini CLI aliases" and m["source_kind"] == "aliases" and m["name"].endswith("(alias)")
                                    and m["reasoning_efforts"] == [] for m in found))
        self.vault.set({"GEMINI_API_KEY": "AIza-api-key-123456"})
        with patch("agentflow.providers.http_json", side_effect=DomainError("HTTP 403", 502)):
            self.assertEqual(self.providers.refresh_models("gemini")[0]["source_kind"], "aliases")

    def test_acp_timeout_kills_the_process(self):
        self.scenario(acp={"hang": True})
        started = time.monotonic()
        with self.assertRaises(DomainError) as caught:
            gemini_acp_catalog(self.executable, dict(os.environ), self.providers.workspace, "oauth", timeout=1)
        self.assertEqual(caught.exception.status, 504)
        self.assertLess(time.monotonic() - started, 10)

    def test_probe_fetches_the_catalog_for_a_new_account_and_retries_alias_only_catalogs_rarely(self):
        self.sign_in()
        self.scenario(acp={"error": True})
        self.providers._probe_status("gemini")
        self.assertEqual({m["source_kind"] for m in self.providers.models if m["provider"] == "gemini"}, {"aliases"})
        self.assertFalse(self.providers._gemini_catalog_due())
        self.providers._gemini_catalog_at -= 1900
        self.assertTrue(self.providers._gemini_catalog_due())
        self.scenario(acp={"models": ACP_MODELS})
        self.providers._probe_status("gemini")
        self.assertEqual({m["source_kind"] for m in self.providers.models if m["provider"] == "gemini"}, {"acp"})
        self.assertFalse(self.providers._gemini_catalog_due())


class InferenceTests(GeminiBase):
    def test_plain_prompt_command_environment_and_usage(self):
        self.catalog()
        self.scenario(turns=[{"text": '{"reply":"hello","state":"complete"}'}])
        output = self.providers.infer(self.actor(), "Plain prompt", "run-plain")
        self.assertEqual(output, '{"reply":"hello","state":"complete"}')
        call = self.log("run")[0]
        args = call["args"]
        self.assertEqual(args[:2], ["-p", ""])
        self.assertEqual(args[args.index("-o") + 1], "stream-json")
        self.assertEqual(args[args.index("-m") + 1], "gemini-test-pro")
        self.assertEqual(args[args.index("--approval-mode") + 1], "default")
        self.assertTrue(args[args.index("--policy") + 1].endswith("gemini-policy.toml"))
        self.assertEqual(args[args.index("-e") + 1], "none")
        self.assertIn("--allowed-mcp-server-names", args)
        self.assertNotIn("-r", args)
        self.assertEqual(call["stdin"], "Plain prompt")
        self.assertTrue(call["system_md"].endswith("worker-instructions.md"))
        self.assertIsNone(call["api_key"])
        self.assertEqual(self.providers.account_runs["run-plain"]["usage"],
                         {"input_tokens": 200, "cached_input_tokens": 800, "cache_creation_tokens": 0, "output_tokens": 50})
        self.assertEqual(self.providers.account_runs["run-plain"]["actual_models"], ["gemini-test-pro"])
        self.assertIn("gemini:gemini-test-pro", self.providers.verified)

    def test_api_key_mode_passes_the_key_to_gemini_only(self):
        self.vault.set({"GEMINI_API_KEY": "AIza-api-key-123456"})
        self.scenario(acp={"models": ACP_MODELS})
        self.providers.refresh_models("gemini")
        self.providers.infer(self.actor(), "Plain prompt", "run-key")
        call = self.log("run")[0]
        self.assertEqual((call["api_key"], call["auth_type"]), ("AIza-api-key-123456", "gemini-api-key"))

    def test_usage_shapes_from_stream_and_json_stats(self):
        usage = Providers._gemini_usage
        self.assertEqual(usage({"models": {"a": {"input_tokens": 100, "cached": 40, "output_tokens": 7, "total_tokens": 150},
                                           "b": {"input_tokens": 10, "cached": 0, "output_tokens": 1, "input": 10}}})[0],
                         {"input_tokens": 70, "cached_input_tokens": 40, "cache_creation_tokens": 0, "output_tokens": 51})
        self.assertEqual(usage({"models": {"m": {"tokens": {"prompt": 500, "candidates": 20, "total": 560, "cached": 100, "thoughts": 40}}}}),
                         ({"input_tokens": 400, "cached_input_tokens": 100, "cache_creation_tokens": 0, "output_tokens": 60}, ["m"]))
        self.assertEqual(usage({"input": 30, "cached": 70, "output_tokens": 5})[0]["input_tokens"], 30)
        self.assertEqual(usage({"duration_ms": 3}), (None, None))
        self.assertEqual(usage(None), (None, None))

    def test_native_session_first_turn_then_resume_with_only_the_delta(self):
        self.catalog()
        self.scenario(turns=[{"text": "first"}, {"text": "second"}])
        first = self.providers.infer(self.actor(input_images=[]), self.bundle(volatile="FULL CONTEXT"), "run-native")
        self.assertEqual(first, "first")
        call = self.log("run")[0]
        self.assertNotIn("-r", call["args"])
        self.assertEqual((call["stdin"], call["system_text"]), ("FULL CONTEXT", "SYSTEM PROMPT"))
        self.assertIn(os.path.join("prompts", "d" * 16 + ".md"), call["system_md"])
        info = self.providers.account_runs["run-native"]["native"]
        self.assertFalse(info["resumed"])
        session_id = info["session_id"]
        self.assertTrue(session_id)
        self.assertIn(("gemini", "run-native", session_id), [(e["provider"], e["run_id"], e["session_id"]) for e in self.providers.sessions.entries()])
        second = self.providers.infer(self.actor(), self.bundle(delta="ONLY THE DELTA", resume_id=session_id), "run-native")
        self.assertEqual(second, "second")
        call = self.log("run")[1]
        self.assertEqual(call["args"][call["args"].index("-r") + 1], session_id)
        self.assertEqual(call["stdin"], "ONLY THE DELTA")
        info = self.providers.account_runs["run-native"]["native"]
        self.assertEqual((info["resumed"], info["reset"], info["session_id"]), (True, False, session_id))

    def test_resume_failure_falls_back_to_a_fresh_full_prompt(self):
        self.catalog()
        self.scenario(turns=[{"fail_resume": True}, {"text": "recovered"}])
        output = self.providers.infer(self.actor(), self.bundle(volatile="FULL AGAIN", delta="DELTA", resume_id="11111111-2222-3333-4444-555555555555"), "run-fallback")
        self.assertEqual(output, "recovered")
        calls = self.log("run")
        self.assertEqual(len(calls), 2)
        self.assertIn("-r", calls[0]["args"])
        self.assertNotIn("-r", calls[1]["args"])
        self.assertEqual(calls[1]["stdin"], "FULL AGAIN")
        info = self.providers.account_runs["run-fallback"]["native"]
        self.assertEqual((info["resumed"], info["reset"]), (False, True))

    def test_images_are_attached_as_at_references_once(self):
        self.catalog()
        pictures = self.root / "my pictures"
        pictures.mkdir()
        image = pictures / "shot one.png"
        image.write_bytes(b"\x89PNG fake")
        picture = {"path": str(image), "mime": "image/png"}
        self.scenario(turns=[{"text": "seen"}, {"text": "again"}])
        self.providers.infer(self.actor(input_images=[picture]), self.bundle(volatile="LOOK"), "run-image")
        call = self.log("run")[0]
        reference = "@" + image.resolve().as_posix().replace(" ", "\\ ")
        self.assertIn(reference, call["stdin"])
        self.assertTrue(call["stdin"].startswith("LOOK"))
        self.assertEqual(call["args"][call["args"].index("--include-directories") + 1], str(image.resolve().parent))
        info = self.providers.account_runs["run-image"]["native"]
        self.assertEqual(info["image_keys"], [image_key(picture)])
        self.providers.infer(self.actor(input_images=[picture]), self.bundle(delta="MORE", resume_id=info["session_id"], sent=[image_key(picture)]), "run-image")
        again = self.log("run")[1]
        self.assertEqual(again["stdin"], "MORE")
        self.assertNotIn("--include-directories", again["args"])

    def test_chat_with_gemini_accepts_image_attachments(self):
        # The former guard ("Gemini CLI does not support image attachments") is gone.
        self.catalog()
        chunk = lambda kind, data: struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
        raw = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress((b"\x00" + b"\x00\xff\x00" * 2) * 2)) + chunk(b"IEND", b""))
        attachment = self.engine.attachments.upload({"name": "pixel.png", "data": base64.b64encode(raw).decode()})
        self.store.settings({"execution_enabled": True})
        self.store.patch("agents", "a-lead", {"provider": "gemini", "model": "gemini-test-pro", "binding_origin": "owner"})
        run = self.engine.chat({"agent_id": "a-lead", "message": "What is in this image?", "channel": "dm-a-lead",
                                "attachment_ids": [attachment["id"]]})
        self.assertEqual(run["attachment_ids"], [attachment["id"]])
        pictures = self.engine.attachments.images(run)
        self.assertEqual(len(pictures), 1)
        self.providers.infer(self.actor(input_images=pictures), "What is in this image?", "run-chat-image")
        self.assertIn("@" + Path(pictures[0]["path"]).resolve().as_posix(), self.log("run")[0]["stdin"])

    def test_stream_parsing_chunks_tool_commentary_and_errors(self):
        self.catalog()
        self.scenario(turns=[{"chunks": ['{"reply":', '"joined"}']}])
        self.assertEqual(self.providers.infer(self.actor(), "p", "r1"), '{"reply":"joined"}')
        events = [{"type": "init", "session_id": "$SID", "model": "m"},
                  {"type": "message", "role": "assistant", "content": "Let me use a tool.", "delta": True},
                  {"type": "tool_use", "tool_name": "run_shell_command", "tool_id": "t1", "parameters": {}},
                  {"type": "tool_result", "tool_id": "t1", "status": "error", "output": "denied by policy"},
                  {"type": "error", "severity": "warning", "message": "Retrying after 429 (transient)"},
                  {"type": "message", "role": "assistant", "content": "FINAL", "delta": True},
                  {"type": "result", "status": "success", "stats": {}}]
        self.scenario(turns=[{"events": events}])
        self.assertEqual(self.providers.infer(self.actor(), "p", "r2"), "FINAL")
        self.scenario(turns=[{"raw": json.dumps({"response": "from json", "stats": {"models": {"m": {"tokens": {"prompt": 10, "candidates": 2, "total": 12, "cached": 4}}}}})}])
        self.assertEqual(self.providers.infer(self.actor(), "p", "r3"), "from json")
        self.assertEqual(self.providers.account_runs["r3"]["usage"]["input_tokens"], 6)
        for turn in ({"events": [{"type": "init", "session_id": "$SID"}, {"type": "result", "status": "success"}]},
                     {"events": [{"type": "message", "role": "assistant", "content": "partial", "delta": True},
                                 {"type": "result", "status": "error", "error": {"type": "Error", "message": "model exploded"}}]},
                     {"raw": "", "exit": 1, "stderr": "boom"},
                     {"raw": "", "exit": 41, "stderr": "Please set an Auth method"}):
            with self.subTest(turn=turn):
                self.scenario(turns=[turn])
                with self.assertRaises(DomainError) as caught:
                    self.providers.infer(self.actor(), "p", "r4")
                self.assertNotIsInstance(caught.exception, ProviderLimit)
        self.assertEqual(caught.exception.status, 401)

    def test_malformed_session_ids_are_never_stored_or_passed_on(self):
        self.catalog()
        events = [{"type": "init", "session_id": "bad id & calc %PATH%", "model": "m"},
                  {"type": "message", "role": "assistant", "content": "ok", "delta": True}, {"type": "result", "status": "success"}]
        self.scenario(turns=[{"events": events}])
        self.providers.infer(self.actor(), self.bundle(), "run-badsid")
        self.assertIsNone(self.providers.account_runs["run-badsid"]["native"]["session_id"])
        self.assertEqual(self.providers.sessions.entries(), [])

    def test_windows_cmd_shim_spawn_goes_through_cmd_exe_for_gemini_only(self):
        from agentflow import native
        wrap = lambda executable, args: native.launch_args(executable, args, windows=True)
        process = unittest.mock.MagicMock(returncode=0)
        process.communicate.return_value = ("", "")
        with patch("agentflow.providers.launch_args", side_effect=wrap), patch("agentflow.providers.subprocess.Popen", return_value=process) as popen:
            self.providers._spawn([r"C:\npm\gemini.cmd", "-p", "", "-m", "pro"], "stdin", "run-shim", shim=True)
            self.providers._spawn([r"C:\bin\claude.cmd", "--system-prompt", 'text with "quotes" and %PATH%'], "stdin", "run-claude")
        self.assertEqual(popen.call_args_list[0].args[0], 'cmd.exe /d /s /c "C:\\npm\\gemini.cmd -p "" -m pro"')
        self.assertEqual(popen.call_args_list[1].args[0][0], r"C:\bin\claude.cmd")

    def test_secret_in_output_is_redacted(self):
        self.catalog()
        self.vault.set({"GEMINI_API_KEY": "AIza-secret-key-123456"})
        self.scenario(turns=[{"text": "echo AIza-secret-key-123456"}])
        self.assertNotIn("AIza-secret-key-123456", self.providers.infer(self.actor(), "p", "r5"))

    def test_unlisted_model_images_and_bad_ids_are_refused(self):
        self.catalog()
        from agentflow.models import ModelUnavailable
        with self.assertRaises(ModelUnavailable):
            self.providers.infer(self.actor(model="not-in-catalog"), "p", "r6")
        with self.assertRaises(ModelUnavailable):
            self.providers.infer(self.actor(model="bad id; rm -rf"), "p", "r7")
        self.assertEqual(self.log("run"), [])

    def test_signed_out_account_never_spawns(self):
        self.catalog()
        for name in ("oauth_creds.json", "google_accounts.json"):
            (self.profile / name).unlink()
        with self.assertRaises(DomainError) as caught:
            self.providers.infer(self.actor(), "p", "r8")
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(self.log("run"), [])


class QuotaTests(GeminiBase):
    def run_error(self, message, **extra):
        self.catalog()
        self.scenario(turns=[{"events": [{"type": "init", "session_id": "$SID", "model": "m"},
                                         {"type": "error", "severity": "error", "message": message},
                                         {"type": "result", "status": "error", "error": {"type": "ApiError", "message": message}}],
                              "exit": 1, **extra}])
        return self.providers.infer(self.actor(), "p", "run-quota")

    def test_short_retry_delay_is_a_transient_limit(self):
        with self.assertRaises(TransientLimit):
            self.run_error("[429 Too Many Requests] RESOURCE_EXHAUSTED: Quota exceeded for metric requests per minute. Please retry in 41.5s.")
        self.assertEqual(self.providers.quota_blocks, {})

    def test_long_reset_becomes_a_usage_cap_with_a_reset_time(self):
        before = time.time()
        with self.assertRaises(ProviderLimit) as caught:
            self.run_error("You have exhausted your capacity on this model. Your quota will reset after 3h12m5s.")
        self.assertAlmostEqual(caught.exception.retry_at, before + 3 * 3600 + 12 * 60 + 5, delta=90)  # the planner adds a 30 s pad
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(len(self.providers.quota_blocks), 1)
        with self.assertRaises(ProviderLimit):  # the stored block gates the next attempt without spawning
            self.providers.infer(self.actor(), "p", "run-again")
        self.assertEqual(len(self.log("run")), 1)

    def test_unknown_reset_quota_error_is_a_usage_cap(self):
        with self.assertRaises(ProviderLimit) as caught:
            self.run_error("RESOURCE_EXHAUSTED: daily quota exceeded for Gemini requests")
        self.assertTrue(caught.exception.retry_at)  # the planner always assigns a probe time

    def test_stderr_429_with_exit_code_is_detected_and_retry_parsing(self):
        with self.assertRaises(TransientLimit):
            self.catalog()
            self.scenario(turns=[{"raw": "", "exit": 1, "stderr": 'Error 429: {"error":{"status":"RESOURCE_EXHAUSTED","details":[{"retryDelay":"30s"}]}}'}])
            self.providers.infer(self.actor(), "p", "run-stderr")
        self.assertEqual(gemini_retry_seconds('{"retryDelay":"30s"}'), 30)
        self.assertEqual(gemini_retry_seconds("Please retry in 800ms"), 0.8)
        self.assertEqual(gemini_retry_seconds("quota will reset after 1h 2m"), 3720)
        self.assertIsNone(gemini_retry_seconds("no hint"))


class RouteTests(GeminiBase):
    def serve(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service, "token", 0))
        port = server.server_port
        server.RequestHandlerClass = make_handler(self.service, "token", port)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        conn = http.client.HTTPConnection("127.0.0.1", port)
        self.addCleanup(conn.close)
        def post(path, body, cookie="af_session=token"):
            conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json", "Cookie": cookie,
                                                          "Origin": f"http://127.0.0.1:{port}"})
            response = conn.getresponse()
            return response.status, response.read().decode()
        return post

    def test_key_route_stores_in_vault_refreshes_catalog_and_never_echoes(self):
        self.sign_in()
        post = self.serve()
        key = "AIzaRouteKey1234567890"
        page = {"models": [{"name": "models/gemini-3-flash-image", "supportedGenerationMethods": ["generateContent"]}]}
        with patch("agentflow.providers.http_json", return_value=page):
            status, body = post("/api/providers/gemini/key", {"api_key": key})
        self.assertEqual(status, 200)
        self.assertNotIn(key, body)
        result = json.loads(body)
        self.assertEqual((result["saved"], result["api_key_set"], result["auth"], result["models"], result["error"]), (True, True, "api_key", 3, None))
        self.assertEqual(self.vault.get("GEMINI_API_KEY"), key)
        self.assertEqual({m["id"] for m in self.providers.models if m["provider"] == "gemini"}, {"gemini-test-pro", "gemini-test-flash", "gemini-3-flash-image"})
        self.assertNotIn(key, json.dumps(self.store.snapshot()))
        self.assertNotIn(key, self.providers.catalog_path.read_text())
        state = self.service.store.snapshot()
        self.assertNotIn("GEMINI_API_KEY", json.dumps(state))
        status, body = post("/api/providers/gemini/key", {"api_key": None})
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual((result["api_key_set"], result["auth"]), (False, "oauth"))
        self.assertEqual(self.vault.get("GEMINI_API_KEY"), "")
        self.assertEqual({m["id"] for m in self.providers.models if m["provider"] == "gemini"}, {"gemini-test-pro", "gemini-test-flash"})
        for bad in ({"api_key": "has space 12345"}, {"api_key": 12345}, {}):
            self.assertEqual(post("/api/providers/gemini/key", bad)[0], 400)

    def test_key_route_requires_an_owner_session(self):
        post = self.serve()
        self.assertEqual(post("/api/providers/gemini/login", {}, cookie="af_session=wrong")[0], 401)
        self.assertEqual(post("/api/providers/gemini/key", {"api_key": "AIzaNoSession123456"}, cookie="")[0], 401)
        self.assertEqual(self.vault.get("GEMINI_API_KEY"), "")

    def test_login_route_launches_a_visible_console_on_windows_and_explains_elsewhere(self):
        post = self.serve()
        status, body = post("/api/providers/gemini/login", {})
        result = json.loads(body)
        self.assertEqual((status, result["launched"]), (200, False))
        self.assertIn("gemini", result["instructions"])
        with patch("agentflow.providers._is_windows", return_value=True), patch.object(self.providers, "_launch_console") as launch:
            status, body = post("/api/providers/gemini/login", {})
        self.assertEqual((status, json.loads(body)), (200, {"launched": True}))
        launch.assert_called_once_with(["cmd", "/c", "start", "Gemini sign-in", self.executable])
        self.providers.cli["gemini"] = None
        self.assertEqual(post("/api/providers/gemini/login", {})[0], 409)

    def test_models_refresh_route_serves_gemini(self):
        self.sign_in()
        post = self.serve()
        status, body = post("/api/models/refresh", {"provider": "gemini"})
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in json.loads(body)["models"]], ["gemini-test-pro", "gemini-test-flash"])


if __name__ == "__main__":
    unittest.main()
