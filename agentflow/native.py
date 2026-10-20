"""Discover installed vendor CLIs and read their supported control metadata."""
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .store import DomainError


def finish_metadata_process(process,reader=None):
    """Bound metadata cleanup even when a descendant retains the stdout pipe."""
    try:
        if os.name=='nt' and isinstance(process.pid,int):
            subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,timeout=5,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except (OSError,subprocess.TimeoutExpired):pass
    try:
        if process.poll() is None:process.kill()
        process.wait(timeout=5)
    except (OSError,subprocess.TimeoutExpired):pass
    if reader:reader.join(timeout=1)
    # BufferedIO.close acquires the reader's lock. Closing from this thread while
    # readline waits on an inherited pipe can block forever, even after proc.kill.
    # The daemon reader owns stdout closure instead.
    process.stdin.close()


def runnable_cli(path):
    """Prefer a runnable npm shim: `gemini.ps1` -> sibling `gemini.cmd` when it exists."""
    if path and str(path).lower().endswith(".ps1"):
        sibling = Path(path).with_suffix(".cmd")
        if sibling.is_file():
            return str(sibling)
    return path


def launch_args(executable, args, windows=None):
    """Popen argument (list, or one string for npm .cmd shims) that runs `executable args` with shell=False.

    Windows npm shims (`gemini.cmd`) cannot be started as plain executables: they go through
    `cmd.exe /d /s /c "<exe> args"` (the extra outer quotes are what /s strips). `.ps1` falls back to PowerShell.
    Arguments are quoted with list2cmdline; cmd would still expand `%VAR%` or break on `"`, so those are refused."""
    windows = os.name == "nt" if windows is None else windows
    suffix = Path(str(executable)).suffix.lower()
    args = [str(a) for a in args]
    if not windows:
        return [executable, *args]
    if suffix in (".cmd", ".bat"):
        if any(c in a for a in [str(executable), *args] for c in '"%\r\n'):
            raise DomainError("A command argument contains characters that cannot be passed to a Windows .cmd launcher safely.", 400)
        return 'cmd.exe /d /s /c "' + subprocess.list2cmdline([str(executable), *args]) + '"'
    if suffix == ".ps1":
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(executable), *args]
    return [executable, *args]


def discover_cli(provider):
    explicit = os.getenv(provider.upper() + "_PATH")
    if explicit:
        return runnable_cli(explicit if Path(explicit).is_file() else shutil.which(explicit))
    found = shutil.which(provider)
    if found:
        return runnable_cli(found)
    home = Path(os.getenv("USERPROFILE") or Path.home())
    candidates = [home / ".local" / "bin" / (provider + ".exe")]
    if provider == "gemini":
        # npm -g on Windows puts shims in %APPDATA%\npm; .cmd is the runnable one.
        for base in dict.fromkeys(b for b in (os.getenv("APPDATA"), str(home / "AppData" / "Roaming")) if b):
            candidates += [Path(base) / "npm" / ("gemini" + ext) for ext in (".cmd", ".exe", ".ps1")]
        return runnable_cli(str(next((p for p in candidates if p.is_file()), "")) or None)
    if provider == "claude":
        for editor in (".vscode", ".vscode-insiders"):
            candidates += list((home / editor / "extensions").glob(
                "anthropic.claude-code-*/resources/native-binary/claude.exe"))
    if provider == "codex":
        candidates += list((home / ".vscode" / "extensions").glob(
            "openai.chatgpt-*/bin/windows-x86_64/codex.exe"))
    def version(path):
        return tuple(int(n) for n in re.findall(r"\d+", str(path)))
    return str(next((p for p in sorted(candidates, key=version, reverse=True) if p.is_file()), "")) or None


def claude_flags(persist=False):
    # Keep native login, but disable host customizations, hooks, MCP and tools.
    # Metadata calls never persist a session; inference calls opt in when native
    # sessions are enabled so later turns can use --resume with only the delta.
    flags = ["--safe-mode", "--tools", "", "--permission-mode", "dontAsk",
             "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
    return flags if persist else flags + ["--no-session-persistence"]


def claude_account(executable, environment):
    try:
        result = subprocess.run([executable, "auth", "status", "--json"], env=environment,
            capture_output=True, text=True, encoding="utf-8", timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        data = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise DomainError("Unable to read the current Claude Code account through auth status.", 502) from None
    if not data.get("loggedIn"):
        return None
    return {"type": data.get("authMethod"), "email": data.get("email"),
            "organization": data.get("orgName"), "org_id": data.get("orgId"),
            "subscription_type": data.get("subscriptionType"), "config_directory": data.get("configDirectory")}


def claude_catalog(executable, environment):
    args = [executable, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", *claude_flags()]
    process = subprocess.Popen(args, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    messages = queue.Queue()
    def reader():
        try:
            for line in process.stdout:
                try:messages.put(json.loads(line))
                except ValueError:continue
        finally:
            process.stdout.close()
            messages.put(None)
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        process.stdin.write(json.dumps({"type": "control_request", "request_id": "agentflow-catalog",
            "request": {"subtype": "initialize", "hooks": {}}}) + "\n")
        process.stdin.flush()
        while True:
            message = messages.get(timeout=25)
            if message is None:
                raise DomainError("Claude Code did not return its SDK model catalog. Check the version and sign-in.", 502)
            response = message.get("response", {})
            if message.get("type") == "control_response" and response.get("request_id") == "agentflow-catalog":
                if response.get("subtype") != "success":
                    raise DomainError("Claude Code rejected the model catalog request for the current account.", 502)
                return response.get("response", {}).get("models", [])
    except (queue.Empty, BrokenPipeError):
        raise DomainError("Timed out reading the Claude Code model catalog.", 504) from None
    finally:
        finish_metadata_process(process,thread)


GEMINI_ALIASES = (("auto", "Auto", "Chooses between Pro and Flash per request."),
                  ("pro", "Pro", "Most capable Gemini model the CLI resolves for this account."),
                  ("flash", "Flash", "Fast, balanced Gemini model."),
                  ("flash-lite", "Flash-Lite", "Fastest, lowest-cost Gemini model."))
ACP_AUTH_METHODS = {"oauth": "oauth-personal", "api_key": "gemini-api-key", "vertex": "vertex-ai"}


def gemini_acp_catalog(executable, environment, cwd, auth_mode=None, timeout=20):
    """Ask `gemini --experimental-acp` (Agent Client Protocol, newline-delimited JSON-RPC) for its models.

    initialize -> (authenticate when the agent lists the method) -> session/new {cwd, mcpServers: []}. The result's
    `models.availableModels[{modelId, name, description}]` is returned as [{id, name, description, current}], or []
    when the agent reports no model list. Raises DomainError on spawn failure, protocol errors or after `timeout`."""
    Path(cwd).mkdir(parents=True, exist_ok=True)
    try:
        process = subprocess.Popen(launch_args(executable, ["--experimental-acp"]), cwd=str(cwd), env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        raise DomainError("Cannot start the Gemini CLI. Check the installation.", 502) from None
    messages = queue.Queue()
    def reader():
        try:
            for line in process.stdout:
                try:messages.put(json.loads(line))
                except ValueError:continue
        finally:
            process.stdout.close()
            messages.put(None)
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    def send(message):
        process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        process.stdin.flush()
    def call(request_id, method, params):
        send({"id": request_id, "method": method, "params": params})
        while True:
            message = messages.get(timeout=max(0.05, deadline - time.monotonic()))
            if message is None:
                raise DomainError("The Gemini CLI closed its protocol session before returning a model list.", 502)
            if not isinstance(message, dict):
                continue
            if message.get("method") and message.get("id") is not None:
                send({"id": message["id"], "error": {"code": -32601, "message": "Not supported by this client."}})
            elif message.get("id") == request_id:
                if "error" in message:
                    raise DomainError("The Gemini CLI rejected " + method + ".", 502)
                return message.get("result") or {}
    try:
        initialized = call(1, "initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}}})
        wanted = ACP_AUTH_METHODS.get(auth_mode)
        offered = [m.get("id") for m in initialized.get("authMethods") or [] if isinstance(m, dict)]
        if wanted and wanted in offered:
            try:call(2, "authenticate", {"methodId": wanted})
            except DomainError:pass  # session/new reports the real problem if authentication was required
        created = call(3, "session/new", {"cwd": str(cwd), "mcpServers": []})
        models = created.get("models") if isinstance(created.get("models"), dict) else {}
        current = models.get("currentModelId")
        return [{"id": str(m.get("modelId") or m.get("id")), "name": str(m.get("name") or m.get("modelId") or m.get("id")),
                 "description": str(m.get("description") or ""), "current": (m.get("modelId") or m.get("id")) == current}
                for m in models.get("availableModels") or [] if isinstance(m, dict) and (m.get("modelId") or m.get("id"))]
    except (queue.Empty, BrokenPipeError, OSError):
        raise DomainError("Timed out reading the Gemini CLI model list.", 504) from None
    finally:
        finish_metadata_process(process, thread)
