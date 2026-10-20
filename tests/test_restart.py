"""Restart robustness: the helper's command line and environment, its wait/retry/failure logic (with a fake System, so no
process is started and nothing sleeps), the fast exit of the old runtime. No real token or secret appears here."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agentflow import restarter  # noqa: E402
import server  # noqa: E402

spec = importlib.util.spec_from_file_location("restart_helper", ROOT / "scripts" / "restart_helper.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

FAKE_TOKEN = "123456:FAKE_TEST_TOKEN_not_a_real_secret_000"
WIN_ROOT = r"C:\Team Apps - AgentFlow-Demo"


class CommandLineTests(unittest.TestCase):
    def test_server_command_keeps_the_exact_interpreter_and_original_arguments(self):
        with tempfile.TemporaryDirectory(prefix="Team Apps - AgentFlow-Demo ") as folder:
            script = Path(folder) / "server.py"
            script.write_text("")
            data = str(Path(folder) / ".agentflow")
            command = restarter.server_command(9000, [str(script), "--port", "9000", "--data-dir", data], executable="/py/python 3.12/python")
        self.assertEqual(command, ["/py/python 3.12/python", str(script.resolve()), "--port", "9000", "--data-dir", data])

    def test_server_command_falls_back_to_this_checkout_outside_server_py(self):
        command = restarter.server_command(8123, ["pytest"], executable="python")
        self.assertEqual(command, ["python", str(ROOT / "server.py"), "--port", "8123"])

    def test_helper_command_with_spaces_and_dashes_round_trips_through_windows_quoting(self):
        python = WIN_ROOT + r"\.venv\Scripts\python.exe"
        server_cmd = [python, WIN_ROOT + r"\server.py", "--port", "8765", "--data-dir", WIN_ROOT + r"\.agentflow"]
        command = restarter.helper_command(4242, 8765, WIN_ROOT + r"\.agentflow", WIN_ROOT + r"\.agentflow\restart-env.json", server_cmd,
                                           python=python, helper=WIN_ROOT + r"\scripts\restart_helper.py", cwd=WIN_ROOT)
        self.assertEqual(command[:2], [python, WIN_ROOT + r"\scripts\restart_helper.py"])
        self.assertEqual(command[command.index("--pid") + 1], "4242")
        self.assertEqual(command[command.index("--cwd") + 1], WIN_ROOT)
        self.assertEqual(command[command.index("--") + 1:], server_cmd)
        line = subprocess.list2cmdline(command)
        self.assertIn('"%s"' % (WIN_ROOT + r"\server.py"), line)
        self.assertIn('--cwd "%s"' % WIN_ROOT, line)
        args, rest = helper.parse(command[2:])   # what the helper itself reads back
        self.assertEqual((args.pid, args.port, args.cwd, args.data_dir), (4242, 8765, WIN_ROOT, WIN_ROOT + r"\.agentflow"))
        self.assertEqual(rest, server_cmd)

    def test_wmi_script_is_one_line_and_quotes_apostrophes(self):
        command = ["C:\\Users\\O'Neil\\python.exe", WIN_ROOT + r"\scripts\restart_helper.py", "--", "x y"]
        script = restarter.cim_script(command, WIN_ROOT)
        self.assertNotIn("\n", script)
        self.assertIn("Win32_Process", script)
        self.assertIn("ShowWindow=[uint16]0", script)
        self.assertIn("CurrentDirectory='%s'" % WIN_ROOT, script)
        self.assertIn("O''Neil", script)
        self.assertTrue(script.endswith("exit [int]$r.ReturnValue"))
        self.assertEqual(restarter.powershell_command(script)[-1], script)


class EnvironmentTests(unittest.TestCase):
    def test_snapshot_excludes_secrets_and_keeps_what_the_new_server_needs(self):
        environ = {"PATH": "C:\\Windows", "USERPROFILE": "C:\\Users\\me", "APPDATA": "x", "LOCALAPPDATA": "y", "HOME": "h", "PYENV_ROOT": "p",
                   "PYENV": "p", "CODEX_PATH": "c", "CLAUDE_PATH": "c", "GEMINI_PATH": "g", "AGENTFLOW_MODE": "work",
                   "TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "GATEWAY_API_KEY": "k", "FACEBOOK_APP_SECRET": "s", "DB_PASSWORD": "p",
                   "OPENAI_API_KEY": "sk-fake", "MY_CUSTOM_VAR": "ok", "HUGE": "x" * 9000}
        result = restarter.safe_env(environ, secret_names=["MY_CUSTOM_VAR"])
        for name in ("PATH", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOME", "PYENV_ROOT", "PYENV", "CODEX_PATH", "CLAUDE_PATH", "GEMINI_PATH", "AGENTFLOW_MODE"):
            self.assertIn(name, result)
        for name in ("TELEGRAM_BOT_TOKEN", "GATEWAY_API_KEY", "FACEBOOK_APP_SECRET", "DB_PASSWORD", "OPENAI_API_KEY", "MY_CUSTOM_VAR", "HUGE"):
            self.assertNotIn(name, result)
        self.assertNotIn(FAKE_TOKEN, json.dumps(result))

    def test_env_file_round_trip_with_a_bom_and_bad_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = restarter.write_env(Path(folder) / "env.json", {"PATH": "a", "TELEGRAM_BOT_TOKEN": FAKE_TOKEN})
            self.assertNotIn(FAKE_TOKEN, path.read_text(encoding="utf-8"))
            bom = Path(folder) / "bom.json"
            bom.write_bytes(b"\xef\xbb\xbf" + json.dumps({"AF_TEST_ENV": "1"}).encode())
            messages = []
            try:
                self.assertEqual(helper.load_env(str(bom), messages.append), 1)
                self.assertEqual(os.environ.get("AF_TEST_ENV"), "1")
            finally:
                os.environ.pop("AF_TEST_ENV", None)
            self.assertEqual(helper.load_env(str(Path(folder) / "missing.json"), messages.append), 0)
            self.assertTrue(messages)
            self.assertEqual(helper.load_env("", messages.append), 0)


class FakeSystem:
    """Scripted clock, sleep and process operations. `ready_after` is the attempt number that finally answers."""

    def __init__(self, ready_after=1, exits=True, free_after=0.0, spawn_errors=(), fallback_ready=False):
        self.now, self.sleeps, self.spawned, self.ran = 0.0, [], [], []
        self.ready_after, self.exits, self.free_after = ready_after, exits, free_after
        self.spawn_errors, self.fallback_ready, self.running_fallback = list(spawn_errors), fallback_ready, False
        self.exit_timeouts = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self):
        return self.now

    def wait_exit(self, pid, timeout):
        self.exit_timeouts.append(timeout)
        if not self.exits:
            self.now += timeout
        return self.exits

    def port_free(self, port):
        return self.now >= self.free_after

    def lock_free(self, data_dir):
        return self.now >= self.free_after

    def ready(self, port):
        if self.running_fallback:
            return self.fallback_ready
        return bool(self.spawned) and len(self.spawned) >= self.ready_after and not self.spawn_errors_pending()

    def spawn_errors_pending(self):
        return False

    def spawn(self, argv, cwd, env, log_path):
        if self.spawn_errors:
            raise self.spawn_errors.pop(0)
        self.spawned.append((list(argv), cwd, log_path))
        with open(log_path, "a", encoding="utf-8") as out:
            out.write("Traceback: attempt %d failed to bind\n" % len(self.spawned))
        proc = MagicMock()
        proc.pid = 100 + len(self.spawned)
        proc.poll.return_value = None
        return proc

    def run(self, argv, timeout):
        self.ran.append(argv)
        self.running_fallback = True
        return 0


class RestarterTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.data = self.root / ".agentflow"
        self.data.mkdir()
        self.command = ["/py/python", "/repo/server.py", "--port", "8765", "--data-dir", str(self.data)]

    def make(self, system):
        return helper.Restarter(4242, 8765, str(self.root), self.data, self.command, system=system)

    def log(self):
        return (self.data / "restart.log").read_text(encoding="utf-8")

    def test_first_attempt_starts_the_same_command_in_the_repo_and_logs_each_step(self):
        system = FakeSystem()
        (self.data / "restart-failed.json").write_text("{}")
        self.assertTrue(self.make(system).run())
        self.assertEqual(len(system.spawned), 1)
        argv, cwd, log_path = system.spawned[0]
        self.assertEqual((argv, cwd), (self.command, str(self.root)))
        self.assertEqual(Path(log_path), self.data / "server-start.log")
        self.assertEqual(system.exit_timeouts, [helper.WAIT_EXIT])
        text = self.log()
        for step in ("restart helper started", "waiting up to 30 s for pid 4242", "old process exited", "port and lock are free", "start attempt 1",
                     "server process started", "restart finished"):
            self.assertIn(step, text)
        self.assertRegex(text, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ restart helper started")
        self.assertFalse((self.data / "restart-failed.json").exists())   # a good restart clears an old failure

    def test_waits_for_the_port_and_lock_to_free_up(self):
        system = FakeSystem(free_after=2.0)
        self.assertTrue(self.make(system).run())
        self.assertGreaterEqual(system.now, 2.0)
        self.assertEqual(len(system.spawned), 1)

    def test_old_process_that_never_exits_does_not_block_forever(self):
        system = FakeSystem(exits=False, free_after=0)
        self.assertTrue(self.make(system).run())
        self.assertIn("still running after 30 s", self.log())

    def test_retries_wait_2_5_and_10_seconds_and_log_the_start_log_tail(self):
        system = FakeSystem(ready_after=3)
        self.assertTrue(self.make(system).run())
        self.assertEqual(len(system.spawned), 3)
        self.assertEqual([s for s in system.sleeps if s in helper.RETRY_DELAYS], [2, 5])
        text = self.log()
        self.assertIn("retrying in 2 s", text)
        self.assertIn("retrying in 5 s", text)
        self.assertIn("Traceback: attempt 1 failed to bind", text)   # tail of server-start.log
        self.assertNotIn("retrying in 10 s", text)

    def test_spawn_errors_count_as_failed_attempts(self):
        system = FakeSystem(ready_after=1, spawn_errors=[FileNotFoundError("no python"), PermissionError("denied")])
        self.assertTrue(self.make(system).run())
        text = self.log()
        self.assertIn("could not start the server: FileNotFoundError", text)
        self.assertIn("could not start the server: PermissionError", text)
        self.assertEqual(len(system.spawned), 1)

    def test_total_failure_uses_the_last_resort_then_writes_the_failure_file(self):
        (self.root / "Start-AgentFlow.ps1").write_text("")
        system = FakeSystem(ready_after=99)
        self.assertFalse(self.make(system).run())
        self.assertEqual(len(system.spawned), 4)
        self.assertEqual([s for s in system.sleeps if s in helper.RETRY_DELAYS], [2, 5, 10])
        self.assertEqual(len(system.ran), 1)
        self.assertIn("-NoBrowser", system.ran[0])
        self.assertTrue(system.ran[0][system.ran[0].index("-File") + 1].endswith("Start-AgentFlow.ps1"))
        failed = json.loads((self.data / "restart-failed.json").read_text(encoding="utf-8"))
        self.assertEqual(set(failed), {"at", "reason", "log_tail"})
        self.assertIn("8765", failed["reason"])
        self.assertIn("RESTART FAILED", failed["log_tail"])
        self.assertIn("last resort", failed["log_tail"])

    def test_last_resort_success_counts_as_a_restart(self):
        (self.root / "Start-AgentFlow.ps1").write_text("")
        system = FakeSystem(ready_after=99, fallback_ready=True)
        self.assertTrue(self.make(system).run())
        self.assertFalse((self.data / "restart-failed.json").exists())
        self.assertIn("through Start-AgentFlow.ps1", self.log())

    def test_missing_start_script_is_logged_not_fatal(self):
        system = FakeSystem(ready_after=99)
        self.assertFalse(self.make(system).run())
        self.assertIn("Start-AgentFlow.ps1 not found", self.log())
        self.assertTrue((self.data / "restart-failed.json").exists())

    def test_log_rotates_at_256_kb(self):
        log = helper.Log(self.data / "restart.log")
        (self.data / "restart.log").write_bytes(b"x" * (helper.LOG_LIMIT + 10))
        log("fresh")
        self.assertTrue((self.data / "restart.log.1").exists())
        self.assertEqual(len((self.data / "restart.log").read_text(encoding="utf-8").splitlines()), 1)

    def test_parse_requires_a_command_after_the_separator(self):
        with self.assertRaises(SystemExit):
            helper.parse(["--pid", "1", "--port", "2", "--cwd", "c", "--data-dir", "d"])
        with self.assertRaises(SystemExit):
            helper.parse(["--pid", "1", "--port", "2", "--cwd", "c", "--data-dir", "d", "--"])

    def test_main_returns_the_outcome_and_never_raises(self):
        original = helper.Restarter.run
        try:
            helper.Restarter.run = lambda self: True
            self.assertEqual(helper.main(["--pid", "1", "--port", "2", "--cwd", str(self.root), "--data-dir", str(self.data), "--", "py"]), 0)
            helper.Restarter.run = lambda self: (_ for _ in ()).throw(RuntimeError("boom"))
            self.assertEqual(helper.main(["--pid", "1", "--port", "2", "--cwd", str(self.root), "--data-dir", str(self.data), "--", "py"]), 1)
        finally:
            helper.Restarter.run = original
        self.assertIn("unexpected error", self.log())

    def test_failure_note_helpers(self):
        self.assertIsNone(restarter.read_failure(self.data))
        (self.data / "restart-failed.json").write_text(json.dumps({"reason": "nope"}))
        self.assertEqual(restarter.read_failure(self.data)["reason"], "nope")
        restarter.clear_failure(self.data)
        self.assertIsNone(restarter.read_failure(self.data))
        (self.data / "restart-failed.json").write_text("not json")
        self.assertIsNone(restarter.read_failure(self.data))


class FastExitTests(unittest.TestCase):
    def test_restart_shutdown_stops_serving_and_forces_the_exit_within_the_deadline(self):
        fake = MagicMock()
        exits = []
        timer = server.restart_shutdown(fake, exit_fn=exits.append, deadline=0.05)
        timer.join(2)
        fake.shutdown.assert_called()
        self.assertEqual(exits, [0])

    def test_the_watchdog_can_be_cancelled_when_cleanup_finishes_first(self):
        exits = []
        timer = server.restart_shutdown(MagicMock(), exit_fn=exits.append, deadline=30)
        timer.cancel()
        self.assertEqual(exits, [])
        import inspect
        self.assertLessEqual(inspect.signature(server.restart_shutdown).parameters["deadline"].default, 5)   # production deadline


if __name__ == "__main__":
    unittest.main()
