"""Server side of a restart: the exact command that starts the helper, the environment it receives and the restart notes.

The helper (scripts/restart_helper.py) runs outside the runtime's process tree. It gets the running interpreter, the
original server command line and a snapshot of the non-secret environment, so the new server starts the way this one did
even though a process created through WMI has a different default environment.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "restart_helper.py"
SECRET_NAME = re.compile(r"(TOKEN|SECRET|PASSW|API_?KEY|APIKEY|CREDENTIAL|BEARER|AUTH|COOKIE|PRIVATE|SESSION_KEY|ACCESS_KEY|LICENSE)", re.I)
KEEP_ALWAYS = ("PATH", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOME", "HOMEDRIVE", "HOMEPATH", "SYSTEMROOT", "SYSTEMDRIVE",
               "COMSPEC", "TEMP", "TMP", "USERNAME", "USERDOMAIN", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "WINDIR",
               "CODEX_PATH", "CLAUDE_PATH", "GEMINI_PATH", "PUBLIC_WEBHOOK_HOST")
MAX_VALUE = 8000


def safe_env(environ, secret_names=()):
    """Environment variables that are safe to write to disk: everything except names that look like credentials, the
    vault's own secret names, and values that contain a known secret. Never raises."""
    hidden = {str(n).upper() for n in secret_names}
    result = {}
    for name, value in dict(environ).items():
        upper = str(name).upper()
        if upper in hidden or (SECRET_NAME.search(upper) and upper not in KEEP_ALWAYS):
            continue
        text = str(value)
        if len(text) > MAX_VALUE:
            continue
        result[str(name)] = text
    return result


def server_command(port, argv=None, executable=None):
    """The command that starts this runtime again: the running interpreter and the original argv (including --port and
    --data-dir). Outside `python server.py ...` (tests, embedding) it falls back to server.py --port <port>."""
    argv = list(sys.argv if argv is None else argv)
    executable = executable or sys.executable
    script = Path(argv[0]) if argv else None
    if script is not None and script.name.lower() == "server.py":
        return [executable, str(script.resolve())] + argv[1:]
    return [executable, str(ROOT / "server.py"), "--port", str(port)]


def helper_command(pid, port, data_dir, env_file, command, python=None, helper=HELPER, cwd=ROOT):
    """python restart_helper.py --pid .. --port .. --cwd .. --data-dir .. --env .. -- <server command>"""
    return [python or sys.executable, str(helper), "--pid", str(pid), "--port", str(port), "--cwd", str(cwd), "--data-dir", str(data_dir),
            "--env", str(env_file), "--"] + [str(part) for part in command]


def ps_quote(text):
    return "'" + str(text).replace("'", "''") + "'"


def cim_script(command, cwd):
    """One PowerShell line that creates the process through WMI (so it is not a child of the runtime), hidden, and exits
    with WMI's return value."""
    line = subprocess.list2cmdline([str(part) for part in command])
    return ("$s = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}; "
            "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=%s; CurrentDirectory=%s; ProcessStartupInformation=$s}; "
            "exit [int]$r.ReturnValue" % (ps_quote(line), ps_quote(cwd)))


def powershell_command(script):
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", script]


def write_env(path, environ, secret_names=()):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(safe_env(environ, secret_names)), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def read_failure(data_dir):
    """{at, reason, log_tail} left by a helper that could not restart the runtime, else None."""
    path = Path(data_dir) / "restart-failed.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"reason": "The last restart did not complete."}
    except (OSError, ValueError):
        return None


def clear_failure(data_dir):
    try:
        (Path(data_dir) / "restart-failed.json").unlink()
    except OSError:
        pass
