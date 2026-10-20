#!/usr/bin/env python3
"""Restart helper: waits for the old AgentFlow runtime to exit, starts it again and checks that it answers.

Stdlib only. Started by the runtime (Engine.request_restart) or by scripts/Restart-AgentFlow.ps1 outside the runtime's
process tree:

    python restart_helper.py --pid 1234 --port 8765 --cwd C:\\repo --data-dir C:\\repo\\.agentflow [--env env.json] -- <python> <server.py> --port 8765

Everything after `--` is the exact command that starts the runtime. Every step is logged with a timestamp to
<data-dir>/restart.log; the server's own output goes to <data-dir>/server-start.log. When all attempts fail a
<data-dir>/restart-failed.json is written, which the next runtime start shows to the owner.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LOG_LIMIT = 256 * 1024
WAIT_EXIT = 30
WAIT_FREE = 30
WAIT_READY = 30
RETRY_DELAYS = (2, 5, 10)
NT = os.name == "nt"
CREATE_NO_WINDOW, DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP, BREAKAWAY = 0x08000000, 0x00000008, 0x00000200, 0x01000000


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Log:
    def __init__(self, path):
        self.path = Path(path)

    def __call__(self, message):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > LOG_LIMIT:
                self.path.replace(self.path.with_name(self.path.name + ".1"))
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write("%s %s\n" % (stamp(), message))
        except OSError:
            pass

    def tail(self, limit=2000):
        try:
            return self.path.read_text(encoding="utf-8", errors="replace")[-limit:]
        except OSError:
            return ""


class System:
    """The real operations. Tests replace this class with a fake, so no process is started or waited for."""

    def sleep(self, seconds):
        time.sleep(seconds)

    def clock(self):
        return time.monotonic()

    def wait_exit(self, pid, timeout):
        """True when the process is gone (or was never there)."""
        if NT:
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(0x00100000, False, int(pid))   # SYNCHRONIZE
                if not handle:
                    if kernel32.GetLastError() == 87:   # ERROR_INVALID_PARAMETER: there is no such process
                        return True
                    raise OSError("OpenProcess failed")   # access denied: fall back to polling
                try:
                    return kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == 0
                finally:
                    kernel32.CloseHandle(handle)
            except Exception:
                pass
        end = self.clock() + timeout
        while self.clock() < end:
            if not self.alive(pid):
                return True
            self.sleep(0.2)
        return not self.alive(pid)

    @staticmethod
    def alive(pid):
        try:
            os.kill(int(pid), 0)
            return True
        except ProcessLookupError:
            return False
        except OSError:
            return NT   # Windows: os.kill(pid, 0) is not a probe; treat errors as "still there" and let the port decide

    def port_free(self, port):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", int(port)))
            return True
        except OSError:
            return False
        finally:
            probe.close()

    def lock_free(self, data_dir):
        path = Path(data_dir) / "runtime.lock"
        if not path.exists():
            return True
        try:
            handle = path.open("a+b")
        except OSError:
            return False
        try:
            handle.seek(0)
            if NT:
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return True
        except OSError:
            return False
        finally:
            handle.close()

    def ready(self, port):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/" % int(port), timeout=2) as response:
                return response.status == 200
        except Exception:
            return False

    def spawn(self, argv, cwd, env, log_path):
        """Start the runtime detached, with stdout and stderr appended to the start log."""
        with open(log_path, "ab") as out:
            out.write(("\n--- %s start: %s\n" % (stamp(), subprocess.list2cmdline(argv))).encode("utf-8", "replace"))
            out.flush()
            flags = (CREATE_NO_WINDOW | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP) if NT else 0
            options = dict(cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, close_fds=True)
            if NT:
                try:
                    return subprocess.Popen(argv, creationflags=flags | BREAKAWAY, **options)
                except OSError:
                    return subprocess.Popen(argv, creationflags=flags, **options)   # a job object without breakaway
            return subprocess.Popen(argv, start_new_session=True, **options)

    def run(self, argv, timeout):
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).returncode


class Restarter:
    def __init__(self, pid, port, cwd, data_dir, command, system=None, log=None):
        self.pid, self.port, self.cwd, self.data_dir, self.command = pid, int(port), str(cwd), Path(data_dir), list(command)
        self.system = system or System()
        self.log = log or Log(self.data_dir / "restart.log")
        self.start_log = self.data_dir / "server-start.log"
        self.failed_file = self.data_dir / "restart-failed.json"

    def start_tail(self, limit=1500):
        try:
            return self.start_log.read_text(encoding="utf-8", errors="replace")[-limit:]
        except OSError:
            return ""

    def wait_free(self):
        """Port free and runtime.lock acquirable; True when both were seen before the timeout."""
        system, end = self.system, self.system.clock() + WAIT_FREE
        while True:
            port, lock = system.port_free(self.port), system.lock_free(self.data_dir)
            if port and lock:
                return True
            if system.clock() >= end:
                self.log("port free: %s, runtime.lock free: %s after %d s" % (port, lock, WAIT_FREE))
                return False
            system.sleep(0.3)

    def wait_ready(self, proc):
        system, end = self.system, self.system.clock() + WAIT_READY
        while system.clock() < end:
            if system.ready(self.port):
                return True
            code = proc.poll() if proc is not None else None
            if code is not None:
                self.log("the new server exited early with code %s" % code)
                return False
            system.sleep(0.5)
        return False

    def attempt(self, number):
        self.log("start attempt %d: %s" % (number, subprocess.list2cmdline(self.command)))
        try:
            proc = self.system.spawn(self.command, self.cwd, os.environ.copy(), str(self.start_log))
        except Exception as exc:   # missing python, bad path, permissions
            self.log("could not start the server: %s: %s" % (type(exc).__name__, exc))
            return False
        self.log("server process started (pid %s); waiting up to %d s for http://127.0.0.1:%d/" % (getattr(proc, "pid", "?"), WAIT_READY, self.port))
        if self.wait_ready(proc):
            return True
        self.log("no answer; server-start.log ends with:\n" + self.start_tail())
        try:
            if proc.poll() is None:
                proc.terminate()
                self.log("stopped the unresponsive new server")
        except Exception:
            pass
        return False

    def fallback(self):
        script = Path(self.cwd) / "Start-AgentFlow.ps1"
        if not script.is_file():
            self.log("last resort skipped: Start-AgentFlow.ps1 not found")
            return False
        self.log("last resort: Start-AgentFlow.ps1 -NoBrowser -Port %d" % self.port)
        try:
            code = self.system.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
                                    "-NoBrowser", "-Port", str(self.port)], 120)
        except Exception as exc:
            self.log("Start-AgentFlow.ps1 could not run: %s: %s" % (type(exc).__name__, exc))
            return False
        self.log("Start-AgentFlow.ps1 exit code %s" % code)
        return self.wait_ready(None)

    def fail(self, reason):
        self.log("RESTART FAILED: " + reason)
        try:
            self.failed_file.write_text(json.dumps({"at": stamp(), "reason": reason, "log_tail": self.log.tail(3000)}), encoding="utf-8")
        except OSError:
            self.log("could not write restart-failed.json")

    def run(self):
        system, started = self.system, self.system.clock()
        self.log("restart helper started: old pid %s, port %d, cwd %s" % (self.pid, self.port, self.cwd))
        if self.command and not os.path.isfile(self.command[0]):
            self.log("warning: %s does not exist" % self.command[0])
        self.log("waiting up to %d s for pid %s to exit" % (WAIT_EXIT, self.pid))
        exited = system.wait_exit(self.pid, WAIT_EXIT)
        self.log("old process exited" if exited else "old process is still running after %d s; the port and lock decide" % WAIT_EXIT)
        free = self.wait_free()
        self.log("port and lock are free" if free else "port or lock still busy; trying anyway")
        for number in range(1, len(RETRY_DELAYS) + 2):
            if number > 1:
                delay = RETRY_DELAYS[number - 2]
                self.log("retrying in %d s" % delay)
                system.sleep(delay)
            if self.attempt(number):
                self.log("restart finished in %.1f s" % (system.clock() - started))
                try:
                    self.failed_file.unlink()
                except OSError:
                    pass
                return True
        if self.fallback():
            self.log("restart finished through Start-AgentFlow.ps1 in %.1f s" % (system.clock() - started))
            return True
        self.fail("The server did not answer on port %d after %d attempts." % (self.port, len(RETRY_DELAYS) + 1))
        return False


def parse(argv):
    if "--" not in argv:
        raise SystemExit("usage: restart_helper.py --pid N --port N --cwd DIR --data-dir DIR [--env FILE] -- <server command>")
    split = argv.index("--")
    parser = argparse.ArgumentParser(description="AgentFlow restart helper")
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--env", default="")
    args = parser.parse_args(argv[:split])
    command = argv[split + 1:]
    if not command:
        raise SystemExit("no server command after --")
    return args, command


def load_env(path, log):
    if not path:
        return 0
    try:
        values = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        applied = {str(k): str(v) for k, v in values.items() if isinstance(v, (str, int, float))}
        os.environ.update(applied)
        return len(applied)
    except (OSError, ValueError, AttributeError) as exc:
        log("could not read the environment file %s: %s" % (path, type(exc).__name__))
        return 0


def main(argv=None):
    args, command = parse(list(sys.argv[1:] if argv is None else argv))
    log = Log(Path(args.data_dir) / "restart.log")
    try:
        log("environment variables applied: %d" % load_env(args.env, log))
        return 0 if Restarter(args.pid, args.port, args.cwd, args.data_dir, command, log=log).run() else 1
    except Exception:
        log("unexpected error:\n" + traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
