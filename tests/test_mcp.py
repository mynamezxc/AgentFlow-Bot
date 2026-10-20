import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import test_runtime as runtime
from agentflow import mcp
from agentflow.store import DomainError
from server import make_handler

FAKE_SERVER = r'''
import json, os, sys

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

TOOLS = [
    {"name": "echo", "description": "Echo text back.\nSecond line.", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
    {"name": "env", "description": "Return FAKE_SECRET.", "inputSchema": {"type": "object"}},
    {"name": "env_len", "description": "Length of FAKE_SECRET.", "inputSchema": {"type": "object"}},
    {"name": "big", "description": "Large output.", "inputSchema": {"type": "object"}},
    {"name": "image", "description": "Image output.", "inputSchema": {"type": "object"}},
    {"name": "fail", "description": "Tool error.", "inputSchema": {"type": "object"}},
    {"name": "pid", "description": "Process id.", "inputSchema": {"type": "object"}},
]
sys.stdout.write("starting up (banner on stdout is ignored)\n")
sys.stdout.flush()
for line in sys.stdin:
    try:
        message = json.loads(line)
    except ValueError:
        continue
    method, ident = message.get("method"), message.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": "srv-ping", "method": "ping"})
        send({"jsonrpc": "2.0", "id": ident, "result": {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
              "serverInfo": {"name": "fake", "version": "1"}}})
    elif ident == "srv-ping" and "result" in message:
        open(os.environ.get("FAKE_LOG", os.devnull), "a").write("pong\n")
    elif method == "tools/list":
        cursor = (message.get("params") or {}).get("cursor")
        if not cursor:
            send({"jsonrpc": "2.0", "id": ident, "result": {"tools": TOOLS[:3], "nextCursor": "page2"}})
        else:
            send({"jsonrpc": "2.0", "id": ident, "result": {"tools": TOOLS[3:]}})
    elif method == "tools/call":
        params = message["params"]
        name, arguments = params["name"], params.get("arguments") or {}
        if name == "echo":
            content = [{"type": "text", "text": "echo: " + str(arguments.get("text"))}]
        elif name == "env":
            content = [{"type": "text", "text": os.environ.get("FAKE_SECRET", "")}]
        elif name == "env_len":
            content = [{"type": "text", "text": "len=" + str(len(os.environ.get("FAKE_SECRET", "")))},
                       {"type": "text", "text": "child_env_has_openai=" + str("OPENAI_API_KEY" in os.environ)}]
        elif name == "big":
            content = [{"type": "text", "text": "x" * 100000}]
        elif name == "image":
            content = [{"type": "text", "text": "before"}, {"type": "image", "mimeType": "image/png", "data": "QUJD" * 100}]
        elif name == "pid":
            content = [{"type": "text", "text": str(os.getpid())}]
        else:
            send({"jsonrpc": "2.0", "id": ident, "result": {"content": [{"type": "text", "text": "boom"}], "isError": True}})
            continue
        send({"jsonrpc": "2.0", "id": ident, "result": {"content": content}})
    elif ident is not None and method:
        send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "unknown method"}})
'''


class FakeHttpMcp:
    """Local Streamable-HTTP MCP server. /json replies with JSON, /sse with event streams."""

    def __init__(self):
        self.log, self.initializes, self.expire_once = [], 0, False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def reply(self, code, body=None, headers=None, sse=False):
                self.send_response(code)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                if body is None:
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                raw = json.dumps(body).encode()
                if sse:
                    raw = b": comment\n\nevent: message\ndata: " + json.dumps({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}).encode() + b"\n\nevent: message\ndata: " + raw + b"\n\n"
                    self.send_header("Content-Type", "text/event-stream")
                else:
                    self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(raw)

            def do_DELETE(self):
                owner.log.append(("DELETE", self.headers.get("Mcp-Session-Id")))
                self.reply(200)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                method, ident = body.get("method"), body.get("id")
                owner.log.append((method, self.headers.get("Mcp-Session-Id"), self.headers.get("X-Api-Key"), self.headers.get("MCP-Protocol-Version")))
                if self.path == "/auth":
                    return self.reply(401, {"error": "nope"})
                if self.headers.get("X-Api-Key") != "key-123456":
                    return self.reply(403, {"error": "bad key"})
                sse = self.path == "/sse"
                if method == "initialize":
                    owner.initializes += 1
                    return self.reply(200, {"jsonrpc": "2.0", "id": ident, "result": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "serverInfo": {"name": "http-fake"}}}, {"Mcp-Session-Id": "sess-" + str(owner.initializes)}, sse)
                if self.headers.get("Mcp-Session-Id") != "sess-" + str(owner.initializes):
                    return self.reply(404, {"error": "unknown session"})
                if method == "notifications/initialized":
                    return self.reply(202)
                if method == "tools/list":
                    return self.reply(200, {"jsonrpc": "2.0", "id": ident, "result": {"tools": [
                        {"name": "add", "description": "Add numbers", "inputSchema": {"type": "object"}}]}}, sse=sse)
                if method == "tools/call":
                    if owner.expire_once:
                        owner.expire_once = False
                        return self.reply(404, {"error": "session expired"})
                    args = body["params"]["arguments"]
                    return self.reply(200, {"jsonrpc": "2.0", "id": ident, "result": {"content": [
                        {"type": "text", "text": "sum=" + str(args.get("a", 0) + args.get("b", 0))}]}}, sse=sse)
                self.reply(200, {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "no such method"}}, sse=sse)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path):
        return "http://127.0.0.1:" + str(self.port) + path

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class McpTests(unittest.TestCase):
    setUp = runtime.RuntimeTests.setUp
    ready = runtime.RuntimeTests.ready

    def tearDown(self):
        self.engine.mcp.close()
        runtime.RuntimeTests.tearDown(self)

    @property
    def script(self):
        path = self.root / "fake_mcp.py"
        if not path.exists():
            path.write_text(FAKE_SERVER, encoding="utf-8")
        return str(path)

    def stdio(self, name="Fake", **extra):
        return self.engine.mcp.save({"name": name, "transport": "stdio", "command": sys.executable, "args": [self.script], **extra})

    def tool(self, run, name, args):
        actor = self.store.get("agents", run["agent_id"])
        return self.engine.tool(actor, {"name": name, "arguments": args}, run)

    def run_work(self):
        self.ready()
        return self.engine.chat({"agent_id": "a-lead", "message": "Use the connected tools"})

    def call(self, run, server, tool, arguments=None):
        return self.tool(run, "mcp.call", {"server_id": server["id"], "tool": tool, "arguments": arguments or {}})

    # ---- stdio --------------------------------------------------------------------------------------------

    def test_stdio_handshake_pagination_call_and_env(self):
        log = self.root / "pong.log"
        server = self.stdio(env={"FAKE_SECRET": "s3cret-value-123", "FAKE_LOG": str(log)})
        self.assertEqual(server["status"], "untested")
        tested = self.engine.mcp.test(server["id"])
        self.assertEqual((tested["status"], tested["tool_count"], tested["error"]), ("ok", 7, ""))
        self.assertEqual([t["name"] for t in tested["tools"]][:3], ["echo", "env", "env_len"])
        client = self.engine.mcp.clients[server["id"]]
        self.assertEqual(client.protocol, "2025-03-26")  # falls back to what the server speaks
        self.assertEqual(log.read_text().strip(), "pong")  # the client answered the server's ping request
        run = self.run_work()
        self.engine.mcp.save({"id": server["id"], "trusted": True})
        result = self.call(run, server, "echo", {"text": "hi"})
        self.assertEqual((result["text"], result["is_error"], result["truncated"]), ("echo: hi", False, False))
        result = self.call(run, server, "env_len")
        self.assertIn("len=16", result["text"])
        self.assertIn("child_env_has_openai=False", result["text"])
        self.assertEqual(self.call(run, server, "env")["text"], "[secret]")  # secret values never reach agents
        self.assertTrue(self.call(run, server, "fail")["is_error"])

    def test_output_cap_and_image_notes(self):
        server = self.stdio(trusted=True)
        self.engine.mcp.test(server["id"])
        run = self.run_work()
        big = self.call(run, server, "big")
        self.assertTrue(big["truncated"])
        self.assertLess(len(big["text"]), 40100)
        self.assertIn("truncated", big["text"])
        image = self.call(run, server, "image")["text"]
        self.assertIn("before", image)
        self.assertIn("image content omitted: image/png", image)
        self.assertNotIn("QUJD", image)

    def test_idle_close_and_respawn_and_close_all(self):
        server = self.stdio(trusted=True)
        self.engine.mcp.test(server["id"])
        run = self.run_work()
        first_pid = self.call(run, server, "pid")["text"]
        client = self.engine.mcp.clients[server["id"]]
        self.assertIsNone(client.proc.poll())
        self.assertEqual(self.call(run, server, "pid")["text"], first_pid)  # kept alive while in use
        self.engine.mcp.idle_seconds = -1
        self.assertEqual(self.engine.mcp.sweep(), 1)
        self.assertIsNotNone(client.proc.poll())
        self.assertEqual(self.engine.mcp.clients, {})
        self.engine.mcp.idle_seconds = 600
        self.assertNotEqual(self.call(run, server, "pid")["text"], first_pid)  # respawned on demand
        spawned = self.engine.mcp.clients[server["id"]]
        self.engine.mcp.close()
        self.assertIsNotNone(spawned.proc.poll())

    def test_busy_server_is_not_idle_closed(self):
        server = self.stdio(trusted=True)
        self.engine.mcp.test(server["id"])
        client = self.engine.mcp.clients[server["id"]]
        client.busy = 1
        self.engine.mcp.idle_seconds = -1
        self.assertEqual(self.engine.mcp.sweep(), 0)
        client.busy = 0

    def test_missing_command_and_failing_server_report_errors(self):
        server = self.engine.mcp.save({"name": "Missing", "transport": "stdio", "command": "definitely-not-installed-mcp"})
        tested = self.engine.mcp.test(server["id"])
        self.assertEqual(tested["status"], "error")
        self.assertIn("Command not found", tested["error"])
        self.assertEqual(tested["tools"], [])
        broken = self.root / "broken.py"
        broken.write_text("import sys\nsys.stderr.write('missing dependency xyz\\n')\nsys.exit(3)\n")
        server = self.engine.mcp.save({"name": "Broken", "transport": "stdio", "command": sys.executable, "args": [str(broken)]})
        tested = self.engine.mcp.test(server["id"])
        self.assertEqual(tested["status"], "error")
        self.assertIn("missing dependency xyz", tested["error"])

    def test_initialize_timeout(self):
        silent = self.root / "silent.py"
        silent.write_text("import sys,time\nfor line in sys.stdin:\n    time.sleep(30)\n")
        server = self.engine.mcp.save({"name": "Silent", "transport": "stdio", "command": sys.executable, "args": [str(silent)]})
        with patch.object(mcp, "INIT_TIMEOUT", 0.5):
            started = time.monotonic()
            tested = self.engine.mcp.test(server["id"])
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(tested["status"], "error")
        self.assertIn("timed out", tested["error"])

    # ---- HTTP ---------------------------------------------------------------------------------------------

    def http_server(self):
        fake = FakeHttpMcp()
        self.addCleanup(fake.stop)
        return fake

    def test_http_json_and_sse_transports_keep_session_and_headers(self):
        fake = self.http_server()
        for path in ("/json", "/sse"):
            server = self.engine.mcp.save({"name": "Http " + path, "transport": "http", "url": fake.url(path),
                                           "headers": {"X-Api-Key": "key-123456"}, "trusted": True})
            tested = self.engine.mcp.test(server["id"])
            self.assertEqual((tested["status"], [t["name"] for t in tested["tools"]]), ("ok", ["add"]), path)
            run = self.run_work()
            self.assertEqual(self.call(run, server, "add", {"a": 2, "b": 5})["text"], "sum=7")
        calls = [entry for entry in fake.log if entry[0] == "tools/call"]
        self.assertTrue(all(entry[1] == "sess-" + str(i + 1) and entry[2] == "key-123456" and entry[3] == "2025-06-18" for i, entry in enumerate(calls)))
        self.engine.mcp.close()
        self.assertTrue(any(entry[0] == "DELETE" for entry in fake.log))

    def test_http_session_expiry_reinitializes_once(self):
        fake = self.http_server()
        server = self.engine.mcp.save({"name": "Expiring", "transport": "http", "url": fake.url("/json"),
                                       "headers": {"X-Api-Key": "key-123456"}, "trusted": True})
        self.engine.mcp.test(server["id"])
        fake.expire_once = True
        run = self.run_work()
        self.assertEqual(self.call(run, server, "add", {"a": 1, "b": 1})["text"], "sum=2")
        self.assertEqual(fake.initializes, 2)

    def test_http_auth_failures_are_reported_without_secrets(self):
        fake = self.http_server()
        server = self.engine.mcp.save({"name": "OAuth only", "transport": "http", "url": fake.url("/auth")})
        tested = self.engine.mcp.test(server["id"])
        self.assertEqual(tested["status"], "error")
        self.assertIn("OAuth is not supported", tested["error"])
        server = self.engine.mcp.save({"name": "Wrong key", "transport": "http", "url": fake.url("/json"), "headers": {"X-Api-Key": "wrong-key-99"}})
        tested = self.engine.mcp.test(server["id"])
        self.assertIn("HTTP 403", tested["error"])
        self.assertNotIn("wrong-key-99", json.dumps(tested))

    def test_http_unreachable_server(self):
        fake = FakeHttpMcp()
        url = fake.url("/json")
        fake.stop()
        server = self.engine.mcp.save({"name": "Down", "transport": "http", "url": url})
        tested = self.engine.mcp.test(server["id"])
        self.assertEqual(tested["status"], "error")
        self.assertIn("Cannot reach the server", tested["error"])

    # ---- validation ---------------------------------------------------------------------------------------

    def test_validation(self):
        save = self.engine.mcp.save
        bad = [{"name": "", "transport": "stdio", "command": "npx"},
               {"name": "x" * 61, "transport": "stdio", "command": "npx"},
               {"name": "Shell", "transport": "stdio", "command": "npx -y server"},
               {"name": "Shell", "transport": "stdio", "command": "sh;rm"},
               {"name": "Shell", "transport": "stdio", "command": "/bin/sh -c ls"},
               {"name": "Shell", "transport": "stdio", "command": "/no/such/program"},
               {"name": "Shell", "transport": "stdio", "command": "relative/path/tool"},
               {"name": "Shell", "transport": "stdio", "command": "npx", "args": ["a"] * 31},
               {"name": "Shell", "transport": "stdio", "command": "npx", "args": "not a list"},
               {"name": "Env", "transport": "stdio", "command": "npx", "env": {"lower_case": "v"}},
               {"name": "Env", "transport": "stdio", "command": "npx", "env": {"A" * 65: "v"}},
               {"name": "Env", "transport": "stdio", "command": "npx", "env": {"KEY": ""}},
               {"name": "Url", "transport": "http", "url": "http://example.com/mcp"},
               {"name": "Url", "transport": "http", "url": "ftp://127.0.0.1/mcp"},
               {"name": "Url", "transport": "http", "url": "https://user:pw@example.com/mcp"},
               {"name": "Url", "transport": "http", "url": "https:///nohost"},
               {"name": "Hdr", "transport": "http", "url": "https://example.com/mcp", "headers": {"Host": "evil"}},
               {"name": "Hdr", "transport": "http", "url": "https://example.com/mcp", "headers": {"Bad Header": "v"}},
               {"name": "Hdr", "transport": "http", "url": "https://example.com/mcp", "headers": {"X-A": "line\nbreak"}},
               {"name": "Kind", "transport": "ssh", "command": "npx"}]
        for payload in bad:
            with self.assertRaises(DomainError, msg=str(payload)[:80]):
                save(payload)
        self.assertEqual(self.engine.mcp.servers(), [])
        for url in ("https://example.com/mcp", "http://127.0.0.1:9/mcp", "http://localhost:9/mcp", "http://[::1]:9/mcp"):
            save({"name": url, "transport": "http", "url": url})
        save({"name": "Plain", "transport": "stdio", "command": "npx", "args": ["-y", "pkg"], "env": {"API_KEY_2": "abcd1234"}})
        with self.assertRaises(DomainError):  # names are unique, case-insensitively
            save({"name": "plain", "transport": "stdio", "command": "npx"})

    def test_secrets_are_write_only(self):
        server = self.engine.mcp.save({"name": "Secrets", "transport": "stdio", "command": "npx",
                                       "env": {"TOKEN_A": "alpha-secret-1", "TOKEN_B": "bravo-secret-2"}})
        key = "MCP_" + server["id"] + "_TOKEN_A"
        self.assertEqual(self.vault.get(key), "alpha-secret-1")
        self.assertEqual(server["env_keys"], [{"key": "TOKEN_A", "set": True}, {"key": "TOKEN_B", "set": True}])
        self.assertEqual(server["header_keys"], [])
        blobs = [json.dumps(self.engine.mcp.listing()), json.dumps(self.store.snapshot()), json.dumps(self.engine.mcp.agent_list({}))]
        for blob in blobs:
            self.assertNotIn("alpha-secret-1", blob)
            self.assertNotIn("bravo-secret-2", blob)
        # '' keeps the stored value, an omitted key is deleted, a new value replaces it.
        updated = self.engine.mcp.save({"id": server["id"], "env": {"TOKEN_A": "", "TOKEN_C": "charlie-secret-3"}})
        self.assertEqual([k["key"] for k in updated["env_keys"]], ["TOKEN_A", "TOKEN_C"])
        self.assertEqual(self.vault.get(key), "alpha-secret-1")
        self.assertFalse(self.vault.values.get("MCP_" + server["id"] + "_TOKEN_B"))
        self.assertEqual(self.vault.values["MCP_" + server["id"] + "_TOKEN_C"], "charlie-secret-3")
        updated = self.engine.mcp.save({"id": server["id"], "env": {"TOKEN_A": "alpha-new-4"}})
        self.assertEqual(self.vault.values[key], "alpha-new-4")
        # Metadata-only edits keep the recorded tools; connection edits invalidate them.
        self.store.patch("mcp_servers", server["id"], {"status": "ok", "tools": [{"name": "t", "description": "", "input_schema": {}}]})
        self.assertEqual(self.engine.mcp.save({"id": server["id"], "trusted": True})["tool_count"], 1)
        self.assertEqual(self.engine.mcp.save({"id": server["id"], "args": ["--other"]})["status"], "untested")
        removed = self.engine.mcp.remove(server["id"])
        self.assertTrue(removed["removed"])
        self.assertFalse(any(k.startswith("MCP_" + server["id"]) for k in self.vault.values))
        self.assertEqual(self.engine.mcp.servers(), [])

    def test_http_routes_are_owner_api_only(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service, "session", 0))
        port = server.server_port
        server.RequestHandlerClass = make_handler(self.service, "session", port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection("127.0.0.1", port)
        headers = {"Cookie": "af_session=session", "Content-Type": "application/json", "Origin": "http://127.0.0.1:" + str(port)}

        def send(method, path, body=None, auth=True):
            conn.request(method, path, json.dumps(body) if body is not None else None, headers if auth else {"Content-Type": "application/json"})
            res = conn.getresponse()
            return res.status, json.loads(res.read())
        try:
            status, body = send("POST", "/api/mcp/servers", {"name": "Api", "transport": "stdio", "command": sys.executable,
                "args": [self.script], "env": {"FAKE_SECRET": "api-secret-777"}})
            self.assertEqual(status, 200)
            sid = body["id"]
            self.assertNotIn("api-secret-777", json.dumps(body))
            self.assertEqual(send("GET", "/api/mcp/servers", auth=False)[0], 401)
            self.assertEqual(send("POST", "/api/mcp/servers", {"name": "Nope", "transport": "stdio", "command": "x"}, auth=False)[0], 401)
            status, body = send("GET", "/api/mcp/servers")
            self.assertEqual((status, [s["id"] for s in body["servers"]]), (200, [sid]))
            self.assertNotIn("api-secret-777", json.dumps(body))
            status, body = send("POST", "/api/mcp/servers/" + sid + "/test", {})
            self.assertEqual((status, body["status"], body["tool_count"]), (200, "ok", 7))
            self.assertEqual(send("POST", "/api/mcp/servers", {"name": "Bad", "transport": "stdio", "command": "sh -c x"})[0], 400)
            self.assertEqual(send("POST", "/api/mcp/servers/missing/test", {})[0], 404)
            self.assertEqual(send("POST", "/api/mcp/servers/" + sid + "/remove", {}), (200, {"removed": True, "id": sid}))
            self.assertEqual(send("GET", "/api/mcp/servers")[1], {"servers": []})
        finally:
            conn.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    # ---- agent tools --------------------------------------------------------------------------------------

    def test_agents_cannot_add_or_modify_servers(self):
        run = self.run_work()
        server = self.stdio(trusted=True)
        self.engine.mcp.test(server["id"])
        for name in ("mcp.add", "mcp.save", "mcp.remove", "mcp.update"):
            with self.assertRaises(DomainError):
                self.tool(run, name, {"name": "Evil", "command": "sh"})
        with self.assertRaises(DomainError):
            self.tool(run, "mcp.call", {"server_id": server["id"], "tool": "echo", "command": "sh"})
        with self.assertRaises(DomainError):
            self.tool(run, "mcp.list", {"command": "sh", "name": "Evil"})
        with self.assertRaises(DomainError):
            self.call(run, server, "not-a-tool")
        with self.assertRaises(DomainError):
            self.tool(run, "mcp.call", {"server_id": server["id"], "tool": "echo", "arguments": "not-an-object"})
        self.assertEqual(len(self.engine.mcp.servers()), 1)
        self.assertEqual(self.store.get("mcp_servers", server["id"])["command"], sys.executable)

    def test_mcp_list_overview_and_schemas(self):
        run = self.run_work()
        server = self.stdio(name="Docs")
        self.assertEqual(self.tool(run, "mcp.list", {})["servers"][0]["note"][:12], "Not verified")
        self.engine.mcp.test(server["id"])
        listing = self.tool(run, "mcp.list", {})["servers"][0]
        self.assertEqual((listing["name"], listing["status"], listing["trusted"], listing["tool_count"]), ("Docs", "ok", False, 7))
        self.assertEqual(listing["tools"][0], {"name": "echo", "description": "Echo text back."})
        self.assertNotIn("input_schema", json.dumps(listing))
        detail = self.tool(run, "mcp.list", {"server_id": server["id"]})
        self.assertIn("properties", detail["tools"][0]["input_schema"])
        self.engine.mcp.save({"id": server["id"], "enabled": False})
        self.assertEqual(self.tool(run, "mcp.list", {})["servers"], [])
        with self.assertRaises(DomainError):
            self.call(run, server, "echo")

    def test_mcp_list_caps_tools_at_sixty(self):
        run = self.run_work()
        server = self.stdio(name="Many")
        tools = [{"name": "t" + str(i), "description": "Tool " + str(i), "input_schema": {"type": "object"}} for i in range(90)]
        self.store.patch("mcp_servers", server["id"], {"status": "ok", "tools": tools})
        listing = self.tool(run, "mcp.list", {})["servers"][0]
        self.assertEqual((len(listing["tools"]), listing["tool_count"]), (60, 90))
        self.assertIn("more_tools", listing)
        self.assertEqual(len(self.tool(run, "mcp.list", {"server_id": server["id"]})["tools"]), 90)

    def test_existing_agent_without_mcp_in_tools_can_list(self):
        run = self.run_work()
        actor = self.store.patch("agents", "a-lead", {"tools": ["workspace.read"]})
        self.assertEqual(self.engine.tool(actor, {"name": "mcp.list", "arguments": {}}, run), {"servers": [], "hint": mcp.McpManager.agent_list(self.engine.mcp, {})["hint"]})

    # ---- approvals ----------------------------------------------------------------------------------------

    def untrusted(self):
        server = self.stdio(name="Untrusted")
        self.engine.mcp.test(server["id"])
        return server

    def test_untrusted_call_asks_and_allow_once_is_single_use(self):
        server, run = self.untrusted(), self.run_work()
        waiting = self.call(run, server, "echo", {"text": "a"})
        self.assertTrue(waiting["waiting_permission"])
        question = self.store.get("decisions", waiting["decision_id"])
        self.assertEqual((question["agent_id"], question["action_field"], question["action_handler"]), ("a-lead", "mcp_action", "mcp"))
        self.assertEqual([a["value"] for a in question["actions"]], ["allow", "allow_task", "trust", "deny"])
        self.assertIn("echo", question["question"])
        self.assertIn(self.store.get("runs", run["id"])["wait_for_decisions"][0], [question["id"]])
        self.assertEqual(self.call(run, server, "echo", {"text": "a"})["decision_id"], question["id"])  # no duplicate question
        self.engine.team.answer(question["id"], {"answer": "Allow once", "mcp_action": "allow"})
        self.assertEqual(self.call(run, server, "echo", {"text": "a"})["text"], "echo: a")
        again = self.call(run, server, "echo", {"text": "a"})
        self.assertTrue(again["waiting_permission"])
        self.assertNotEqual(again["decision_id"], question["id"])
        self.assertFalse(self.store.get("mcp_servers", server["id"])["trusted"])

    def test_deny_blocks_the_call(self):
        server, run = self.untrusted(), self.run_work()
        waiting = self.call(run, server, "echo", {"text": "d"})
        self.engine.team.answer(waiting["decision_id"], {"answer": "Deny", "mcp_action": "deny"})
        result = self.call(run, server, "echo", {"text": "d"})
        self.assertTrue(result["denied"])
        self.assertFalse(result["executed"])
        self.assertNotIn(server["id"], self.engine.mcp.clients.keys() - {server["id"]})

    def test_invalid_action_is_rejected(self):
        server, run = self.untrusted(), self.run_work()
        waiting = self.call(run, server, "echo")
        with self.assertRaises(DomainError):
            self.engine.team.answer(waiting["decision_id"], {"answer": "x", "mcp_action": "root"})
        with self.assertRaises(DomainError):
            self.engine.team.answer(waiting["decision_id"], {"answer": "something else"})
        self.assertEqual(self.store.get("decisions", waiting["decision_id"])["status"], "open")

    def test_allow_task_covers_the_server_until_the_root_run_ends(self):
        server, run = self.untrusted(), self.run_work()
        waiting = self.call(run, server, "echo", {"text": "1"})
        self.engine.team.answer(waiting["decision_id"], {"answer": "Allow this server for this task", "mcp_action": "allow_task"})
        self.assertEqual(self.call(run, server, "echo", {"text": "1"})["text"], "echo: 1")
        self.assertEqual(self.call(run, server, "pid")["is_error"], False)  # other tools on the same server too
        self.assertFalse(self.store.get("mcp_servers", server["id"])["trusted"])
        self.store.patch("runs", run["id"], {"status": "done"})
        self.assertTrue(self.call(run, server, "echo", {"text": "2"})["waiting_permission"])

    def test_trust_persists_on_the_server(self):
        server, run = self.untrusted(), self.run_work()
        waiting = self.call(run, server, "echo", {"text": "t"})
        self.engine.team.answer(waiting["decision_id"], {"answer": "Always trust this server", "mcp_action": "trust"})
        self.assertTrue(self.store.get("mcp_servers", server["id"])["trusted"])
        self.assertTrue(self.engine.mcp.listing()["servers"][0]["trusted"])
        decisions = len(self.store.all("decisions"))
        self.assertEqual(self.call(run, server, "echo", {"text": "other"})["text"], "echo: other")
        self.assertEqual(len(self.store.all("decisions")), decisions)

    def test_auto_mode_skips_the_question_but_logs(self):
        server, run = self.untrusted(), self.run_work()
        self.engine.approvals.set(self.engine.approvals.channel_for(run), "auto")
        before = len(self.store.all("decisions"))
        result = self.call(run, server, "echo", {"text": "auto"})
        self.assertEqual(result["text"], "echo: auto")
        self.assertEqual(len(self.store.all("decisions")), before)
        notices = [m for m in self.store.all("messages") if m.get("kind") == "action" and "Auto-approved" in m["content"]]
        self.assertTrue(notices)
        self.assertEqual(notices[-1]["audience"], "activity")
        self.assertEqual(notices[-1]["actions"][-1]["type"], "mcp")
        self.assertIn("echo", notices[-1]["content"])
        self.assertIn("Untrusted", notices[-1]["content"])

    def test_untrusted_server_never_spawns_before_approval(self):
        server, run = self.untrusted(), self.run_work()
        self.engine.mcp.close()
        with patch.object(self.engine.mcp, "connect", side_effect=AssertionError("spawned")):
            self.assertTrue(self.call(run, server, "echo")["waiting_permission"])

    def test_support_runs_cannot_use_mcp(self):
        server = self.stdio(trusted=True)
        self.engine.mcp.test(server["id"])
        run = {"id": "support-run", "kind": "support", "channel": "dm-a-lead"}
        actor = self.store.get("agents", "a-lead")
        with self.assertRaises(DomainError):
            self.engine.tool(actor, {"name": "mcp.call", "arguments": {"server_id": server["id"], "tool": "echo"}}, run)


if __name__ == "__main__":
    unittest.main()
