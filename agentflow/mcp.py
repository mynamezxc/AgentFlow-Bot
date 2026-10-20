"""Owner-managed MCP (Model Context Protocol) servers and the broker tools that let agents use them.

Only the owner can add, change, test or remove servers (HTTP API + Connections page). Agents get two broker
tools: `mcp.list` (servers and tool names/schemas) and `mcp.call`. Secrets (stdio env values, HTTP header
values) live in the Vault under `MCP_<id>_<KEY>` and are never returned by any API.

Transports: stdio (newline-delimited JSON-RPC 2.0 over a child process) and Streamable HTTP (POST JSON-RPC,
JSON or SSE replies, `Mcp-Session-Id`). OAuth-protected remote servers are not supported (use an API-key header).
"""
import atexit
import hashlib
import http.client
import json
import os
import re
import shutil
import ssl
import subprocess
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlparse

from .contract import TERMINAL
from .store import DomainError, now, uid

PROTOCOL_VERSION = "2025-06-18"
INIT_TIMEOUT = 20
CALL_TIMEOUT = 60
IDLE_SECONDS = 600
OUTPUT_CAP = 40000
MAX_TOOLS = 500
MAX_PAGES = 20
LIST_TOOL_CAP = 60
MAX_SERVERS = 30
MAX_STREAM_BYTES = 8 * 1024 * 1024
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
ENV_KEY = re.compile(r"[A-Z0-9_]{1,64}")
HEADER_KEY = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,63}")
RESERVED_HEADERS = {"host", "content-length", "content-type", "accept", "mcp-session-id", "mcp-protocol-version",
                    "transfer-encoding", "connection", "upgrade"}
PLAIN_COMMAND = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,99}")
SHELLISH = re.compile(r"[\x00-\x1f;|&<>`$\"'%!*?^]")


class McpError(Exception):
    """Protocol/transport failure. `expired` marks an HTTP session the server no longer knows."""
    def __init__(self, message, expired=False):
        super().__init__(message)
        self.expired = expired


# ---- clients ---------------------------------------------------------------------------------------------

class Client:
    """Shared MCP handshake and tool operations on top of a transport's request/notify."""
    kind = ""

    def __init__(self):
        self.busy = 0
        self.last_used = time.monotonic()
        self.protocol = PROTOCOL_VERSION
        self.server_info = {}
        self.closed = False

    def initialize(self):
        result = self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "AgentFlow", "version": "0.3"}}, INIT_TIMEOUT)
        # Servers answer with the version they speak; accept it rather than insisting on ours.
        if isinstance(result.get("protocolVersion"), str) and result["protocolVersion"]:
            self.protocol = result["protocolVersion"]
        if isinstance(result.get("serverInfo"), dict):
            self.server_info = result["serverInfo"]
        self.notify("notifications/initialized")
        return result

    def list_tools(self):
        tools, cursor = [], None
        for _ in range(MAX_PAGES):
            result = self.request("tools/list", {"cursor": cursor} if cursor else {}, INIT_TIMEOUT)
            tools.extend(normalise_tools(result.get("tools")))
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str) or not cursor or len(tools) >= MAX_TOOLS:
                break
        return tools[:MAX_TOOLS]

    def call_tool(self, name, arguments):
        return self.request("tools/call", {"name": name, "arguments": arguments}, CALL_TIMEOUT)

    @property
    def alive(self):
        return not self.closed


class StdioClient(Client):
    kind = "stdio"

    def __init__(self, argv, env, cwd):
        super().__init__()
        self.lock = threading.Lock()
        self.next_id = 1
        self.pending = {}
        self.errors = deque(maxlen=60)
        try:
            self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, cwd=cwd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, ValueError) as exc:
            raise McpError("Cannot start the command: " + str(exc)[:200]) from None
        threading.Thread(target=self._read_stdout, name="mcp-stdout", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="mcp-stderr", daemon=True).start()

    @property
    def alive(self):
        return not self.closed and self.proc.poll() is None

    def stderr_tail(self, limit=600):
        return "\n".join(self.errors)[-limit:]

    def _send(self, message):
        data = (json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with self.lock:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
        except (OSError, ValueError):
            raise McpError("The server process is not accepting input (it exited).") from None

    def _read_stdout(self):
        try:
            while True:
                raw = self.proc.stdout.readline(8 * 1024 * 1024)
                if not raw:
                    break
                try:
                    message = json.loads(raw.decode("utf-8", "replace").strip())
                except ValueError:
                    continue  # banners or logs on stdout are not protocol traffic
                if isinstance(message, dict):
                    self._dispatch(message)
        except (OSError, ValueError):
            pass
        finally:
            self._fail_pending("The server process exited.")

    def _read_stderr(self):
        try:
            for raw in iter(self.proc.stderr.readline, b""):
                self.errors.append(raw.decode("utf-8", "replace").rstrip()[:300])
        except (OSError, ValueError):
            pass

    def _dispatch(self, message):
        if "method" in message:
            if "id" in message:  # server-to-client request: only ping is supported
                reply = {"jsonrpc": "2.0", "id": message["id"]}
                if message["method"] == "ping":
                    reply["result"] = {}
                else:
                    reply["error"] = {"code": -32601, "message": "Method not supported by this client."}
                try:
                    self._send(reply)
                except McpError:
                    pass
            return
        with self.lock:
            slot = self.pending.pop(message.get("id"), None)
        if slot:
            slot[1] = message
            slot[0].set()

    def _fail_pending(self, reason):
        with self.lock:
            slots, self.pending = list(self.pending.values()), {}
        for slot in slots:
            slot[1] = {"error": {"message": reason}}
            slot[0].set()

    def request(self, method, params=None, timeout=CALL_TIMEOUT):
        with self.lock:
            request_id = self.next_id
            self.next_id += 1
            slot = [threading.Event(), None]
            self.pending[request_id] = slot
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            self._send(message)
        except McpError:
            with self.lock:
                self.pending.pop(request_id, None)
            raise
        if not slot[0].wait(timeout):
            with self.lock:
                self.pending.pop(request_id, None)
            try:
                self._send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                            "params": {"requestId": request_id, "reason": "timeout"}})
            except McpError:
                pass
            raise McpError(method + " timed out after " + str(int(timeout)) + " s.")
        return unwrap(slot[1], self.stderr_tail() if not self.alive else "")

    def notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.proc.stdin.close()  # a polite server exits when stdin closes
        except (OSError, ValueError):
            pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                try:  # npx/cmd wrappers leave grandchildren behind; kill the tree
                    subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True,
                                   timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except (OSError, subprocess.SubprocessError):
                    pass
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        self._fail_pending("The connection was closed.")


class HttpClient(Client):
    kind = "http"

    def __init__(self, url, headers):
        super().__init__()
        parts = urlparse(url)
        self.https = parts.scheme == "https"
        self.host, self.port = parts.hostname, parts.port or (443 if self.https else 80)
        self.path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        self.headers = dict(headers)
        self.session_id = None
        self.initialized = False
        self.next_id = 1
        self.lock = threading.Lock()

    def _connection(self, timeout):
        if self.https:
            return http.client.HTTPSConnection(self.host, self.port, timeout=timeout, context=ssl.create_default_context())
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def _headers(self):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **self.headers}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.initialized:
            headers["MCP-Protocol-Version"] = self.protocol
        return headers

    def _post(self, message, timeout, expect_id=None):
        conn = self._connection(timeout)
        deadline = time.monotonic() + timeout
        try:
            conn.request("POST", self.path, body=json.dumps(message, ensure_ascii=False).encode("utf-8"), headers=self._headers())
            response = conn.getresponse()
            session = response.getheader("Mcp-Session-Id")
            if session and message.get("method") == "initialize":
                self.session_id = session
            if response.status in (202, 204):
                response.read(1000)
                return None
            if response.status >= 400:
                body = response.read(600).decode("utf-8", "replace")
                if response.status == 404 and self.session_id:
                    raise McpError("The server no longer knows this session.", expired=True)
                if response.status in (401, 403):
                    raise McpError("HTTP " + str(response.status) + ": the server rejected the credentials. OAuth is not supported; "
                                   "use an API key or token header.")
                raise McpError("HTTP " + str(response.status) + " " + re.sub(r"\s+", " ", body)[:300])
            content_type = (response.getheader("Content-Type") or "").lower()
            if "text/event-stream" in content_type:
                return self._read_stream(response, expect_id, deadline)
            raw = response.read(MAX_STREAM_BYTES + 1)
            if len(raw) > MAX_STREAM_BYTES:
                raise McpError("The server reply is too large.")
            if not raw.strip():
                return None
            try:
                value = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                raise McpError("The server did not return JSON.") from None
            for item in value if isinstance(value, list) else [value]:
                if isinstance(item, dict) and item.get("id") == expect_id and ("result" in item or "error" in item):
                    return item
            return None
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise McpError("Cannot reach the server: " + (str(exc) or type(exc).__name__)[:200]) from None
        finally:
            conn.close()

    @staticmethod
    def _read_stream(response, expect_id, deadline):
        data, total = [], 0

        def complete():
            if not data:
                return None
            text, data[:] = "\n".join(data), []
            try:
                item = json.loads(text)
            except ValueError:
                return None
            for entry in item if isinstance(item, list) else [item]:
                if isinstance(entry, dict) and entry.get("id") == expect_id and ("result" in entry or "error" in entry):
                    return entry
            return None

        while True:
            if time.monotonic() > deadline:
                raise McpError("The event stream timed out.")
            line = response.readline(65536)
            if not line:
                return complete()
            total += len(line)
            if total > MAX_STREAM_BYTES:
                raise McpError("The server reply is too large.")
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if text == "":
                found = complete()
                if found is not None:
                    return found
            elif text.startswith("data:"):
                value = text[5:]
                data.append(value[1:] if value.startswith(" ") else value)

    def request(self, method, params=None, timeout=CALL_TIMEOUT):
        with self.lock:
            request_id = self.next_id
            self.next_id += 1
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        reply = self._post(message, timeout, request_id)
        if reply is None:
            raise McpError("The server sent no reply to " + method + ".")
        if method == "initialize":
            self.initialized = True
            if isinstance(reply.get("result"), dict) and isinstance(reply["result"].get("protocolVersion"), str):
                self.protocol = reply["result"]["protocolVersion"]
        return unwrap(reply)

    def notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._post(message, INIT_TIMEOUT)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.session_id:
            try:  # best-effort session termination (servers may answer 405)
                conn = self._connection(3)
                conn.request("DELETE", self.path, headers=self._headers())
                conn.getresponse().read(200)
                conn.close()
            except (OSError, http.client.HTTPException, ssl.SSLError):
                pass


def unwrap(message, stderr=""):
    if not isinstance(message, dict):
        raise McpError("Malformed reply from the server.")
    if message.get("error") is not None:
        error = message["error"]
        text = str(error.get("message") if isinstance(error, dict) else error)[:400]
        if stderr:
            text += " | " + stderr
        raise McpError(text or "The server returned an error.")
    result = message.get("result")
    return result if isinstance(result, dict) else {}


def normalise_tools(items):
    tools = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not 1 <= len(name) <= 128:
            continue
        description = item.get("description") if isinstance(item.get("description"), str) else ""
        schema = item.get("inputSchema")
        if not isinstance(schema, dict):
            schema = {"type": "object"}
        if len(json.dumps(schema)) > 12000:
            schema = {"type": "object", "description": "Input schema omitted because it is too large."}
        tools.append({"name": name, "description": description[:1000], "input_schema": schema})
    return tools


def format_result(result):
    """Join text content; non-text content becomes a short note. Capped at OUTPUT_CAP bytes."""
    parts = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind in ("image", "audio"):
            size = len(str(item.get("data", ""))) * 3 // 4
            parts.append("[" + kind + " content omitted: " + str(item.get("mimeType", "unknown")) + ", about " + str(size) + " bytes]")
        elif kind == "resource":
            resource = item.get("resource") if isinstance(item.get("resource"), dict) else {}
            if isinstance(resource.get("text"), str):
                parts.append(resource["text"])
            else:
                parts.append("[binary resource omitted: " + str(resource.get("uri", "")) + "]")
        elif kind == "resource_link":
            parts.append("[resource link: " + str(item.get("uri", "")) + "]")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    return "\n".join(parts)


def cap_text(text, limit=OUTPUT_CAP):
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, False
    return raw[:limit].decode("utf-8", "ignore") + "\n[output truncated at " + str(limit // 1000) + " KB]", True


# ---- validation ------------------------------------------------------------------------------------------

def check_command(command):
    if not isinstance(command, str) or not command or command != command.strip() or len(command) > 400:
        raise DomainError("Enter the program to run, for example npx or an absolute path.")
    if PLAIN_COMMAND.fullmatch(command):
        return command
    absolute = PureWindowsPath(command).is_absolute() or PurePosixPath(command).is_absolute()
    if not absolute or SHELLISH.search(command):
        raise DomainError("The command must be a plain program name (such as npx) or an absolute path, not a shell command. Put arguments in Arguments.")
    if not Path(command).is_file():
        raise DomainError("No program was found at this path.")
    return command


def check_url(url):
    parts = urlparse(url) if isinstance(url, str) else None
    try:
        host, _ = (parts.hostname, parts.port) if parts else (None, None)
    except ValueError:
        host = None
    if (not parts or not host or parts.username or parts.password or parts.fragment or len(url) > 2000
            or parts.scheme not in ("https", "http") or (parts.scheme == "http" and host not in LOCAL_HOSTS)):
        raise DomainError("Use an https:// URL, or http:// only for 127.0.0.1 or localhost. Put keys in headers, not in the URL.")
    return url


def vault_key(server_id, key, header=False):
    return "MCP_" + server_id + "_" + ("HDR_" + re.sub(r"[^A-Za-z0-9]", "_", key).upper() if header else key)


class McpManager:
    def __init__(self, engine):
        self.engine, self.store, self.vault = engine, engine.store, engine.vault
        self.lock = threading.RLock()
        self.clients = {}
        self.server_locks = {}
        self.task_grants = set()
        self.idle_seconds = IDLE_SECONDS
        self.sweep_interval = 30
        self._janitor = None
        self._stop = threading.Event()
        engine.decision_handlers["mcp"] = self.approval
        atexit.register(self.close)

    # ---- records ----------------------------------------------------------------------------------------

    def servers(self):
        return [s for s in self.store.all("mcp_servers") if not s.get("removed")]

    def get(self, server_id):
        server = self.store.get("mcp_servers", str(server_id or ""))
        if server.get("removed"):
            raise DomainError("MCP server not found.", 404)
        return server

    def public(self, server):
        secrets = self.vault.values
        tools = server.get("tools", [])
        return {"id": server["id"], "name": server["name"], "transport": server["transport"], "command": server.get("command", ""),
            "args": server.get("args", []), "url": server.get("url", ""), "trusted": bool(server.get("trusted")),
            "enabled": bool(server.get("enabled", True)), "status": server.get("status", "untested"), "error": server.get("error", ""),
            "checked_at": server.get("checked_at"), "version": server.get("version"), "tool_count": len(tools),
            "tools": [{"name": t["name"], "description": t.get("description", "")[:300]} for t in tools],
            "env_keys": [{"key": k, "set": bool(secrets.get(vault_key(server["id"], k)))} for k in server.get("env_keys", [])],
            "header_keys": [{"key": k, "set": bool(secrets.get(vault_key(server["id"], k, True)))} for k in server.get("header_keys", [])]}

    def listing(self):
        return {"servers": [self.public(s) for s in self.servers()]}

    def save(self, data):
        with self.lock:
            previous = self.get(data["id"]) if data.get("id") else None
            if previous and data.get("version") not in (None, previous["version"]):
                raise DomainError("This server changed. Reload before saving.", 409)
            prior = previous or {}
            name = str(data.get("name", prior.get("name", ""))).strip()
            if not 1 <= len(name) <= 60:
                raise DomainError("The server name must contain 1 to 60 characters.")
            if any(s["name"].casefold() == name.casefold() and s["id"] != prior.get("id") for s in self.servers()):
                raise DomainError("Another MCP server already uses this name.")
            if not previous and len(self.servers()) >= MAX_SERVERS:
                raise DomainError("The MCP server limit has been reached. Remove one first.")
            transport = data.get("transport", prior.get("transport", "stdio"))
            if transport not in ("stdio", "http"):
                raise DomainError("Choose Local command or HTTP URL.")
            server_id = prior.get("id") or uid("mcp")
            command, args, url, env, headers = "", [], "", {}, {}
            if transport == "stdio":
                command = check_command(data.get("command", prior.get("command", "")))
                args = data.get("args", prior.get("args", []))
                if (not isinstance(args, list) or len(args) > 30 or any(not isinstance(a, str) or len(a) > 2000 or "\x00" in a for a in args)):
                    raise DomainError("Arguments must be a list of up to 30 text values.")
                env = self.secret_map(data, "env", prior.get("env_keys", []))
                for key in env:
                    if not ENV_KEY.fullmatch(key):
                        raise DomainError("Environment variable names use A-Z, 0-9 and _ (up to 64 characters).")
            else:
                url = check_url(data.get("url", prior.get("url", "")))
                headers = self.secret_map(data, "headers", prior.get("header_keys", []))
                seen = set()
                for key in headers:
                    norm = re.sub(r"[^A-Za-z0-9]", "_", key).upper()
                    if not HEADER_KEY.fullmatch(key) or key.casefold() in RESERVED_HEADERS or norm in seen:
                        raise DomainError("Invalid or duplicate header name: " + key[:40])
                    seen.add(norm)
            if len(env) + len(headers) > 30:
                raise DomainError("At most 30 secrets per server.")
            header = transport == "http"
            supplied, changed = {}, False
            for key, value in (headers if header else env).items():
                slot = vault_key(server_id, key, header)
                if value:
                    changed = True
                    supplied[slot] = value
                elif self.vault.values.get(slot):
                    supplied[slot] = self.vault.values[slot]
                else:
                    raise DomainError("Enter a value for " + key + ".")
            self.vault.set(supplied, replace_prefix="MCP_" + server_id + "_")
            doc = {"name": name, "transport": transport, "command": command, "args": list(args), "url": url,
                   "env_keys": list(env) if not header else [], "header_keys": list(headers) if header else [],
                   "trusted": bool(data.get("trusted", prior.get("trusted", False))),
                   "enabled": bool(data.get("enabled", prior.get("enabled", True))), "removed": False}
            connection = ("transport", "command", "args", "url", "env_keys", "header_keys")
            if not previous or changed or any(doc[k] != prior.get(k) for k in connection):
                doc.update(status="untested", error="", tools=[], checked_at=None)
                self.close_client(server_id)
            if previous and not doc["enabled"]:
                self.close_client(server_id)
            saved = self.store.patch("mcp_servers", server_id, doc) if previous else self.store.create("mcp_servers", {"id": server_id, **doc})
            return self.public(saved)

    @staticmethod
    def secret_map(data, field, previous_keys):
        """{KEY: value}. The payload lists every secret to keep; '' keeps the stored value; omitted keys are deleted."""
        value = data.get(field)
        if value is None:
            return {k: "" for k in previous_keys}
        if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, (str, type(None))) for k, v in value.items()):
            raise DomainError("Secrets must be a map of names to values.")
        if any(len(v or "") > 8000 or "\r" in (v or "") or "\n" in (v or "") for v in value.values()):
            raise DomainError("Secret values must be a single line of up to 8000 characters.")
        return {k.strip(): v or "" for k, v in value.items() if k.strip()}

    def remove(self, server_id):
        with self.lock:
            server = self.get(server_id)
            self.close_client(server["id"])
            self.vault.set({}, replace_prefix="MCP_" + server["id"] + "_")
            with self.store.transaction():
                self.store.patch("mcp_servers", server["id"], {"removed": True, "enabled": False, "tools": [], "command": "",
                    "args": [], "url": "", "env_keys": [], "header_keys": [], "status": "removed", "error": ""})
        return {"removed": True, "id": server["id"]}

    # ---- connections ------------------------------------------------------------------------------------

    def connect(self, server):
        env_secrets = {}
        if server["transport"] == "stdio":
            for key in server.get("env_keys", []):
                env_secrets[key] = self.vault.values.get(vault_key(server["id"], key), "")
            env = {**self.engine.providers.child_environment(), **env_secrets}
            command = check_command(server["command"]) if server.get("command") else ""
            resolved = command if Path(command).is_absolute() else shutil.which(command, path=env.get("PATH"))
            if not resolved:
                raise McpError("Command not found: " + command + ". Install it or enter the full path.")
            client = StdioClient([resolved, *server.get("args", [])], env, str(self.engine.workspace))
        else:
            headers = {k: self.vault.values.get(vault_key(server["id"], k, True), "") for k in server.get("header_keys", [])}
            client = HttpClient(check_url(server["url"]), headers)
        try:
            client.initialize()
        except McpError as exc:
            detail = client.stderr_tail() if isinstance(client, StdioClient) else ""
            client.close()
            raise McpError(str(exc) + (" | " + detail if detail and detail not in str(exc) else "")) from None
        except BaseException:
            client.close()
            raise
        return client

    def server_lock(self, server_id):
        with self.lock:
            return self.server_locks.setdefault(server_id, threading.RLock())

    def acquire(self, server):
        with self.server_lock(server["id"]):
            with self.lock:
                client = self.clients.get(server["id"])
                if client and client.alive:
                    client.busy += 1
                    return client
                self.clients.pop(server["id"], None)
            if client:
                client.close()
            client = self.connect(server)
            with self.lock:
                client.busy += 1
                self.clients[server["id"]] = client
            self.ensure_janitor()
            return client

    @contextmanager
    def session(self, server):
        client = self.acquire(server)
        try:
            yield client
        finally:
            with self.lock:
                client.busy -= 1
                client.last_used = time.monotonic()

    def drop(self, server_id, client=None):
        with self.lock:
            current = self.clients.get(server_id)
            if current is not None and (client is None or current is client):
                self.clients.pop(server_id, None)
            else:
                current = None
        if current is not None:
            current.close()
        elif client is not None:
            client.close()

    def close_client(self, server_id):
        self.drop(server_id)

    def with_retry(self, server, operation):
        for attempt in (0, 1):
            with self.session(server) as client:
                try:
                    return operation(client)
                except McpError as exc:
                    if not exc.expired or attempt:
                        raise
                    self.drop(server["id"], client)

    def ensure_janitor(self):
        with self.lock:
            if self._janitor and self._janitor.is_alive():
                return
            self._stop = threading.Event()
            self._janitor = threading.Thread(target=self._run_janitor, args=(self._stop,), name="mcp-janitor", daemon=True)
            self._janitor.start()

    def _run_janitor(self, stop):
        while not stop.wait(self.sweep_interval):
            self.sweep()

    def sweep(self):
        """Close servers idle for longer than `idle_seconds` (never one with a call in flight)."""
        cutoff = time.monotonic() - self.idle_seconds
        with self.lock:
            stale = [(sid, c) for sid, c in self.clients.items() if c.busy <= 0 and (c.last_used < cutoff or not c.alive)]
            for sid, _ in stale:
                self.clients.pop(sid, None)
        for _, client in stale:
            client.close()
        return len(stale)

    def close(self):
        """Stop all servers (called from Engine shutdown and atexit). Safe to call repeatedly."""
        self._stop.set()
        with self.lock:
            clients, self.clients = list(self.clients.values()), {}
        for client in clients:
            try:
                client.close()
            except Exception:
                pass

    # ---- owner test -------------------------------------------------------------------------------------

    def test(self, server_id):
        server = self.get(server_id)
        with self.server_lock(server["id"]):
            self.close_client(server["id"])
            try:
                client = self.connect(server)
                try:
                    tools = client.list_tools()
                except BaseException:
                    client.close()
                    raise
                with self.lock:
                    self.clients[server["id"]] = client
                self.ensure_janitor()
                changes = {"status": "ok", "error": "", "tools": tools}
            except (McpError, DomainError) as exc:
                changes = {"status": "error", "error": self.vault.redact(str(exc))[:700], "tools": []}
            saved = self.store.patch("mcp_servers", server["id"], {**changes, "checked_at": now()})
        return self.public(saved)

    # ---- agent tools ------------------------------------------------------------------------------------

    def invoke(self, name, args, actor, run):
        if name == "mcp.list":
            return self.agent_list(args)
        if name == "mcp.call":
            return self.agent_call(args, actor, run)
        raise DomainError("Unsupported tool: " + name)

    @staticmethod
    def one_line(text, limit=110):
        line = next((x.strip() for x in str(text or "").splitlines() if x.strip()), "")
        return line if len(line) <= limit else line[:limit - 1] + "…"

    def agent_list(self, args):
        if set(args) - {"server_id"}:
            raise DomainError("mcp.list accepts only server_id. Agents cannot add or change servers; the owner connects them in Connections.")
        if args.get("server_id"):
            server = self.get(args["server_id"])
            tools = server.get("tools", [])
            result = {"id": server["id"], "name": server["name"], "status": server["status"], "trusted": bool(server.get("trusted")),
                      "enabled": bool(server.get("enabled", True)), "tools": tools[:200]}
            if len(tools) > 200:
                result["note"] = "Showing the first 200 of " + str(len(tools)) + " tools."
            return result
        budget, servers = LIST_TOOL_CAP, []
        for server in self.servers():
            if not server.get("enabled", True):
                continue
            tools = server.get("tools", [])
            shown = tools[:max(0, budget)]
            budget -= len(shown)
            entry = {"id": server["id"], "name": server["name"], "status": server["status"], "trusted": bool(server.get("trusted")),
                     "tool_count": len(tools), "tools": [{"name": t["name"], "description": self.one_line(t.get("description"))} for t in shown]}
            if len(shown) < len(tools):
                entry["more_tools"] = "Call mcp.list {server_id} for the full list."
            if server["status"] != "ok":
                entry["note"] = "Not verified. Ask the owner to test it in Connections > MCP servers."
            servers.append(entry)
        return {"servers": servers, "hint": "mcp.list {server_id} returns input schemas; mcp.call {server_id, tool, arguments} runs a tool."}

    def agent_call(self, args, actor, run):
        if set(args) - {"server_id", "tool", "arguments"}:
            raise DomainError("mcp.call accepts only server_id, tool and arguments. Agents cannot add or change servers.")
        server = self.get(args.get("server_id"))
        if not server.get("enabled", True):
            raise DomainError("This MCP server is disabled by the owner.", 403)
        tool, arguments = args.get("tool"), args.get("arguments", {})
        if not isinstance(arguments, dict) or len(json.dumps(arguments, ensure_ascii=False)) > 100000:
            raise DomainError("arguments must be a JSON object up to 100 KB.")
        if not server.get("tools"):
            raise DomainError("This server has no verified tools yet. Ask the owner to test it in Connections > MCP servers.", 409)
        if tool not in {t["name"] for t in server["tools"]}:
            raise DomainError("Unknown tool for this server. Use mcp.list {server_id} to see its tools.")
        gate = self.gate(server, tool, arguments, actor, run)
        if gate is not None:
            return gate
        try:
            result = self.with_retry(server, lambda client: client.call_tool(tool, arguments))
        except McpError as exc:
            raise DomainError('MCP server "' + server["name"] + '": ' + self.vault.redact(str(exc))[:700], 502) from None
        text, truncated = cap_text(self.vault.redact(format_result(result)))
        return {"server": server["name"], "tool": tool, "is_error": bool(result.get("isError")), "text": text, "truncated": truncated}

    # ---- approvals --------------------------------------------------------------------------------------

    def task_allowed(self, run, server_id):
        key = (run.get("root_id", run["id"]), server_id)
        with self.lock:
            if key not in self.task_grants:
                return False
        try:
            alive = self.store.get("runs", key[0])["status"] not in TERMINAL
        except DomainError:
            alive = False
        if not alive:
            with self.lock:
                self.task_grants.discard(key)
        return alive

    def gate(self, server, tool, arguments, actor, run):
        """None = proceed; otherwise the tool result (waiting for the owner, or denied)."""
        if server.get("trusted"):
            return None
        key = hashlib.sha256(json.dumps([run["id"], server["id"], tool, arguments], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        prior = next((q for q in reversed(self.store.all("decisions")) if q.get("mcp_key") == key), None)
        if prior and prior["status"] == "answered":
            if prior.get("mcp_action") == "deny":
                return {"executed": False, "denied": True, "reason": "The owner denied this MCP tool call."}
            if prior.get("mcp_action") == "allow" and not prior.get("mcp_consumed"):
                self.store.patch("decisions", prior["id"], {"mcp_consumed": True})  # single use, never replayed
                return None
        approvals = getattr(self.engine, "approvals", None)
        if (not prior or prior["status"] != "open") and approvals and approvals.auto(run):
            approvals.notify(run, "Call MCP tool " + tool + " on " + server["name"], kind="mcp",
                             detail=json.dumps({"server": server["name"], "tool": tool, "arguments": arguments}, ensure_ascii=False)[:4000])
            return None
        if self.task_allowed(run, server["id"]):
            return None
        if not prior or prior["status"] != "open":
            approval = self.engine.enqueue("a-lead", "Review an MCP tool call request.", "command_approval",
                project_id=run.get("project_id"), root_id=run.get("root_id", run["id"]), channel="dm-a-lead",
                status="waiting_user", system_managed=True)
            actions = [{"value": "allow", "label": "Allow once", "style": "primary", "hint": "Run only this call."},
                {"value": "allow_task", "label": "Allow this server for this task", "style": "quiet", "hint": "Every tool on this server until the task finishes."},
                {"value": "trust", "label": "Always trust this server", "style": "quiet", "hint": "Agents call it without asking. Change this in Connections."},
                {"value": "deny", "label": "Deny", "style": "quiet"}]
            where = server["command"] + " " + " ".join(server.get("args", [])) if server["transport"] == "stdio" else server["url"]
            shown = self.vault.redact(json.dumps(arguments, ensure_ascii=False, indent=1))[:1500]
            asked = self.engine.team.ask(self.store.get("agents", "a-lead"), {
                "question": 'Allow this agent to call "' + tool + '" on the MCP server "' + server["name"] + '"?',
                "context": "@{" + actor["id"] + "}\nServer: " + server["name"] + " (" + where.strip()[:300] + ")\n```json\n" + shown + "\n```\n"
                           "MCP tools run with that server's own access. They are not sandboxed by AgentFlow.",
                "blocking": True, "options": [a["label"] for a in actions], "actions": actions,
                "action_field": "mcp_action", "action_handler": "mcp"}, approval, _routed=True)
            prior = self.store.patch("decisions", asked["decision_id"], {"mcp_request": True, "mcp_key": key,
                "mcp_server_id": server["id"], "mcp_tool": tool, "requester_run_id": run["id"]})
        current = self.store.get("runs", run["id"])
        self.store.patch("runs", run["id"], {"wait_for_decisions": list(dict.fromkeys(current.get("wait_for_decisions", []) + [prior["id"]]))})
        return {"waiting_permission": True, "decision_id": prior["id"], "coordinator_id": "a-lead"}

    def approval(self, question, answer):
        action = answer.get("mcp_action")
        if action not in ("allow", "allow_task", "trust", "deny"):
            raise DomainError("Choose Allow once, Allow this server for this task, Always trust this server, or Deny.")
        if action == "allow_task":
            requester = self.store.get("runs", question["requester_run_id"])
            with self.lock:
                self.task_grants.add((requester.get("root_id", requester["id"]), question["mcp_server_id"]))
        elif action == "trust":
            try:
                self.get(question["mcp_server_id"])
                self.store.patch("mcp_servers", question["mcp_server_id"], {"trusted": True})
            except DomainError:
                pass
        self.store.patch("decisions", question["id"], {"mcp_action": action, "mcp_consumed": action != "allow"})
        self.store.patch("runs", question["run_id"], {"status": "done", "completed_at": now()})
