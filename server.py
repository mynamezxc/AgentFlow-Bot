"""Start with Python 3.12+: python server.py --port 8765 (no packages required)."""
import argparse
import hmac
import json
import mimetypes
import os
import secrets
import sys
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from agentflow.engine import Engine
from agentflow.heartbeat import DEFAULTS as HEARTBEAT_DEFAULTS
from agentflow.providers import Providers, Vault
from agentflow.service import SECRETS, Service
from agentflow.store import DomainError, Store, initial_seed

ROOT = Path(__file__).resolve().parent


def load_env():
    path = ROOT / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class LifetimeLock:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.handle = (directory / "runtime.lock").open("a+b")
        if self.handle.tell() == 0:
            self.handle.write(b"1")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise SystemExit("AgentFlow runtime is already running for this data directory.") from None

    def close(self):
        self.handle.close()


def make_handler(service, session_token, port):
    store, engine, providers, vault = service.store, service.engine, service.providers, service.vault
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    engine.browser.denied_origins.update('http://'+host for host in hosts)
    engine.browser.control_ports.add(port)
    engine.http_port = port
    origins = {"http://" + host for host in hosts}
    public_webhook = os.getenv("PUBLIC_WEBHOOK_HOST", "").strip()

    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentFlow"
        sys_version = ""

        def log_message(self, fmt, *args):
            return

        def send(self, data, code=200, content_type="application/json; charset=utf-8", cookie=False, canvas=False, frame=False):
            raw = json.dumps(data, ensure_ascii=False).encode() if "application/json" in content_type else data
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store" if self.path.startswith("/api") else "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Content-Security-Policy", "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; frame-src 'none'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'" if canvas else "default-src 'none'; frame-ancestors 'self'" if frame else "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: https:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
            if cookie:
                self.send_header("Set-Cookie", f"af_session={session_token}; HttpOnly; SameSite=Strict; Path=/")
            self.end_headers()
            self.wfile.write(raw)

        def authorized(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                value = cookie["af_session"].value if "af_session" in cookie else ""
            except Exception:
                value = ""
            if not hmac.compare_digest(value, session_token):
                raise DomainError("Reopen the app to create a new runtime session.", 401)

        def check_host(self, webhook=False):
            allowed = hosts | ({public_webhook} if webhook and public_webhook else set())
            if self.headers.get("Host") not in allowed:
                raise DomainError("Host not authorized.", 403)
            if self.headers.get("Origin") and self.headers["Origin"] not in origins:
                raise DomainError("Request origin not authorized.", 403)

        def body(self):
            size = int(self.headers.get("Content-Length", "0"))
            route = urlparse(self.path).path
            limit = 12*1024*1024 if route == "/api/attachments" else 2*1024*1024*3 if route.startswith("/api/files/") and route.endswith("/save") else 1100000
            if size < 0 or size > limit:
                raise DomainError("The request exceeds 1 MB.", 413)
            return self.rfile.read(size)

        def do_GET(self):
            try:
                parsed = urlparse(self.path)
                path = unquote(parsed.path)
                self.check_host(path.startswith("/webhooks/"))
                if path == "/webhooks/messenger":
                    query = parse_qs(parsed.query)
                    token = vault.get("META_VERIFY_TOKEN")
                    if token and query.get("hub.mode") == ["subscribe"] and hmac.compare_digest(query.get("hub.verify_token", [""])[0], token):
                        return self.send(query.get("hub.challenge", [""])[0].encode(), content_type="text/plain")
                    raise DomainError("Webhook authentication failed.", 403)
                if path.startswith("/api/"):
                    self.authorized()
                    if path == "/api/health":
                        return self.send({"ok": True, "mode": "local", "version": "0.3.0"})
                    if path in ("/api/state", "/api/export"):
                        snapshot = store.snapshot()
                        snapshot["runtime"] = {"mode": "local", "heartbeat_at": engine.last_heartbeat,
                                               "providers": providers.status(), "models": engine.models.catalog(),
                                               "secrets": {key: bool(vault.get(key)) for key in SECRETS},
                                               "credential_persistence": vault.persistence,
                                               "workspace": str(engine.workspace)}
                        snapshot["runtime"]["plugins"] = engine.plugins.owner_state()
                        snapshot["runtime"]["telegram"] = engine.telegram.public_status()
                        snapshot["runtime"]["heartbeat"] = engine.heartbeat_status()
                        snapshot["runtime"]["heartbeat_config"] = engine.heartbeat.config()
                        snapshot["runtime"]["heartbeat_defaults"] = HEARTBEAT_DEFAULTS
                        return self.send(snapshot)
                    if path == "/api/storage":
                        return self.send(engine.housekeeping.report())
                    if path == "/api/models/history":
                        return self.send(engine.models.history(parse_qs(parsed.query).get("limit", ["50"])[0]))
                    if path == "/api/skills/catalog":
                        query = parse_qs(parsed.query)
                        return self.send(engine.slash.catalog.list(query.get("q", [""])[0][:200], query.get("source", [""])[0][:80]))
                    if path == "/api/mcp/servers":
                        return self.send(engine.mcp.listing())
                    if path == "/api/images/engines":
                        agent_id = parse_qs(parsed.query).get("agent_id", [""])[0]
                        return self.send(engine.images.listing(store.get("agents", agent_id) if agent_id else None))
                    if path == "/api/workspace":
                        files = [{"path": str(p.relative_to(engine.workspace)), "bytes": p.stat().st_size}
                                 for p in engine.workspace.rglob("*") if p.is_file() and not p.is_symlink()
                                 and not any(x.startswith(".") for x in p.relative_to(engine.workspace).parts)][:200]
                        return self.send({"files": files})
                    if path.startswith("/api/attachments/"):
                        item=store.get("attachments",path.split("/")[-1])
                        return self.send(engine.attachments.path(item).read_bytes(),content_type=item["mime"] if item["is_image"] else "application/octet-stream")
                    if path.startswith('/api/files/'):
                        parts=path.strip('/').split('/')
                        if len(parts)!=5 or parts[4]!='preview':raise DomainError('Unknown file endpoint.',404)
                        return self.send(engine.outputs.preview(parts[2],parts[3]))
                    if path.startswith('/api/canvas/'):
                        item,target=engine.outputs.resolve('artifact',path.split('/')[-1],verify=False)
                        if item.get('kind')!='canvas':raise DomainError('This output is not a canvas.',403)
                        return self.send(target.read_bytes(),content_type='text/html; charset=utf-8',canvas=True)
                    if path.startswith('/api/outputs/') and path.endswith('/raw'):
                        item,target=engine.outputs.resolve('artifact',path.split('/')[-2],verify=False)
                        suffix=target.suffix.lower()
                        if suffix=='.pdf':return self.send(target.read_bytes(),content_type='application/pdf',frame=True)  # the preview pane frames it
                        return self.send(target.read_bytes(),content_type=mimetypes.guess_type(str(target))[0] if suffix in ('.png','.jpg','.jpeg','.webp') else 'application/octet-stream')
                    if path.startswith("/api/file_changes/"):
                        return self.send(engine.files.change(path.split("/")[-1]))
                    if path.startswith("/api/artifacts/"):
                        artifact = store.get("artifacts", path.split("/")[-1])
                        if artifact.get("is_image"):
                            return self.send({"artifact": {k:v for k,v in artifact.items() if k != "absolute_path"},"file":{"is_image":True,"url":artifact.get("media_url")}})
                        args = {"path": artifact["path"]}
                        if artifact.get("source") == "project":
                            args.update(project_id=artifact.get("project_id"), grant_id=artifact.get("grant_id"))
                        return self.send({"artifact": {k:v for k,v in artifact.items() if k != "absolute_path"},
                                          "file": engine.files.read(args, project=artifact.get("source") == "project", full=True)})
                    raise DomainError("API endpoint not found.", 404)
                if path.startswith("/media/"):
                    self.authorized()
                    target = (engine.workspace / path.lstrip("/")).resolve()
                    if not target.is_relative_to(engine.workspace / "media") or target.suffix.lower() not in (".png", ".jpg", ".webp"):
                        raise DomainError("Media not found.", 404)
                else:
                    target = (ROOT / "dist" / (path.lstrip("/") or "index.html")).resolve()
                    if not target.is_relative_to(ROOT / "dist"):
                        raise DomainError("Invalid path.", 403)
                if not target.is_file():
                    raise DomainError("Page not found.", 404)
                content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                if target.suffix == ".js":
                    content_type = "text/javascript"
                return self.send(target.read_bytes(), content_type=content_type, cookie=target.name == "index.html")
            except DomainError as exc:
                self.send({"error": str(exc)}, exc.status)
            except (ValueError, TypeError):
                self.send({"error": "Invalid request."}, 400)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self.send({"error": "Unable to read runtime data."}, 500)

        def mutate(self):
            try:
                path = urlparse(self.path).path
                self.check_host(path.startswith("/webhooks/"))
                raw = self.body()
                if path.startswith("/webhooks/") and self.command == "POST":
                    channel = path.rsplit("/", 1)[-1]
                    if channel not in ("telegram", "messenger", "seo"):
                        raise DomainError("Webhook not found.", 404)
                    return self.send(engine.webhook(channel, raw, self.headers))
                self.authorized()
                if self.headers.get("Origin") not in origins:
                    raise DomainError("This action must originate from the local app.", 403)
                if not self.headers.get("Content-Type", "").startswith("application/json"):
                    raise DomainError("The request must use JSON.", 415)
                data = json.loads(raw or b"{}")
                if not isinstance(data, dict):
                    raise DomainError("Invalid JSON content.")
                parts = path.strip("/").split("/")
                if parts[:1] != ["api"]:
                    raise DomainError("API endpoint not found.", 404)
                if path == "/api/settings":
                    mode_before = engine.telegram.mode()
                    result = service.settings(data)
                    if "telegram_mode" in data and engine.telegram.mode() != mode_before:
                        engine.telegram.reload()   # a changed connection mode applies at once, no restart
                    return self.send(result)
                if path == "/api/storage/cleanup":
                    return self.send(engine.housekeeping.cleanup(data.get("categories")))
                if len(parts) == 4 and parts[:2] == ["api", "trash"] and parts[3] == "restore":
                    return self.send(engine.files.restore(parts[2]))
                if path == "/api/heartbeat/pulse-now":
                    return self.send(engine.heartbeat.pulse_now())
                if path == '/api/runtime/stop':
                    return self.send(engine.safety.stop_all())
                if path == "/api/admin/restart":
                    return self.send(engine.request_restart("Restart runtime from the web app"), 202)
                if path == "/api/telegram/test":
                    return self.send(engine.telegram.test())
                if path == "/api/telegram/unlink":
                    return self.send({"unlinked": bool(engine.telegram.linked()) and bool(engine.telegram.unlink())})
                if path == "/api/secrets":
                    supplied = {k: v for k, v in data.items() if k in SECRETS}
                    if supplied.get("TELEGRAM_BOT_TOKEN"):
                        supplied["TELEGRAM_BOT_TOKEN"] = engine.telegram.valid_token(supplied["TELEGRAM_BOT_TOKEN"])
                    saved = vault.set(supplied)
                    if supplied.get("TELEGRAM_BOT_TOKEN"):
                        saved = {**saved, "telegram": engine.telegram.reload()}   # new or replaced token applies at once
                    return self.send(saved)
                if path == "/api/telegram/token-clear":
                    return self.send(engine.telegram.clear_token())
                if path == "/api/chat":
                    return self.send(engine.chat(data), 202)
                if path == "/api/attachments":
                    return self.send(engine.attachments.public(engine.attachments.upload(data)),201)
                if path.startswith('/api/files/') and path.endswith('/open'):
                    if len(parts)!=5:raise DomainError('Unknown file endpoint.',404)
                    return self.send(engine.outputs.open_native(parts[2],parts[3]))
                if path.startswith('/api/files/') and path.endswith('/save'):
                    if len(parts)!=5:raise DomainError('Unknown file endpoint.',404)
                    return self.send(engine.outputs.save(parts[2],parts[3],data))
                if path == "/api/mcp/servers":
                    return self.send(engine.mcp.save(data))
                if len(parts) == 5 and parts[:3] == ["api", "mcp", "servers"] and parts[4] in ("test", "remove"):
                    return self.send(engine.mcp.test(parts[3]) if parts[4] == "test" else engine.mcp.remove(parts[3]))
                if path == "/api/approval-mode":
                    return self.send(engine.approvals.set(data.get("channel"), data.get("mode")))
                if path == "/api/browser/host-map":
                    mapping = engine.browser.set_host_map(data.get("pattern"), data.get("address"))
                    return self.send({"pattern": str(data.get("pattern", "")).strip().lower(), "address": data.get("address") or None, "host_map": mapping})
                if path == "/api/plugins/install":
                    return self.send(engine.plugins.install(data.get("plugin_id"), data.get("enabled", True)))
                if path == "/api/plugins/accounts/save":
                    return self.send(engine.plugins.save_account(data))
                if path == "/api/plugins/accounts/revoke":
                    return self.send(engine.plugins.revoke(data.get("id"), data.get("version")))
                if path == "/api/providers/gemini/login":
                    return self.send(providers.gemini_login())
                if path == "/api/providers/gemini/key":
                    if "api_key" not in data:
                        raise DomainError("Send api_key as a string, or null to remove the stored key.")
                    return self.send(providers.set_gemini_key(data["api_key"]))
                if path == "/api/models/refresh":
                    return self.send({"models": providers.refresh_models(data.get("provider", "gateway"))})
                if path == "/api/skills/import":
                    return self.send(service.import_skill(data), 201)
                if path == "/api/skills/scan":
                    return self.send(engine.skills.scan())
                if path == "/api/skills/install-ref":
                    return self.send(engine.slash.catalog.install(data.get("ref"), data.get("agent_id")), 201)
                if path == "/api/projects/goal":
                    return self.send(engine.slash.goal(data))
                if path == "/api/images/generate":
                    return self.send(engine.generate_image(data), 201)
                if path == "/api/plans":
                    agent = store.get("agents", data.get("agent_id", "a-lead"))
                    return self.send(engine.propose_plan(agent, data), 201)
                if len(parts) == 6 and parts[:2] == ["api", "projects"] and parts[3] == "goals" and parts[5] == "remove" and self.command == "POST":
                    return self.send(service.action("projects", parts[2], "goal-remove", {**data, "goal_id": parts[4]}))
                if len(parts) == 4 and self.command == "POST":
                    return self.send(service.action(parts[1], parts[2], parts[3], data))
                if len(parts) == 2 and self.command == "POST":
                    return self.send(service.create(parts[1], data), 201)
                if len(parts) == 3 and self.command == "PATCH":
                    return self.send(service.update(parts[1], parts[2], data))
                raise DomainError("Operation not supported.", 404)
            except DomainError as exc:
                self.send({"error": vault.redact(str(exc)), **getattr(exc, "extra", {})}, exc.status)
            except (ValueError, TypeError, KeyError, OverflowError):
                self.send({"error": "Invalid data format. Check the fields and try again."}, 400)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self.send({"error": "Unable to save. The result is unconfirmed; reload to reconcile before retrying."}, 500)

        do_POST = mutate
        do_PATCH = mutate

    return Handler


def restart_shutdown(server, exit_fn=os._exit, deadline=4.5):
    """Restart path: stop serving now and make sure the process is gone within `deadline` seconds, whatever a thread is
    doing (the restart helper must never need taskkill). Returns the watchdog timer."""
    watchdog = threading.Timer(deadline, lambda: exit_fn(0))
    watchdog.daemon = True
    watchdog.start()
    threading.Thread(target=server.shutdown, daemon=True, name="agentflow-shutdown").start()
    return watchdog


def main():
    load_env()
    parser = argparse.ArgumentParser(description="AgentFlow local runtime")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=str(ROOT / ".agentflow"))
    parser.add_argument("--no-scheduler", action="store_true")
    args = parser.parse_args()
    data_dir = Path(args.data_dir).resolve()
    lifetime = LifetimeLock(data_dir)
    seed = json.loads((ROOT / "dist" / "seed.json").read_text(encoding="utf-8"))
    store = Store(data_dir, initial_seed(seed))
    vault = Vault(data_dir)
    workspace = ROOT / "workspaces" / "company"
    providers = Providers(store, vault, workspace)
    engine = Engine(store, providers, vault, workspace)
    service = Service(store, providers, vault, engine)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(service, secrets.token_urlsafe(32), args.port))
    restarting = threading.Event()
    engine.on_restart = lambda: (restarting.set(), restart_shutdown(server))
    if not args.no_scheduler:
        engine.start()
    print(f"AgentFlow local: http://127.0.0.1:{args.port}", flush=True)
    print("Durable agent inboxes ready. Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        engine.close()
        store.close()
        lifetime.close()
        if restarting.is_set():
            sys.stdout.flush()
            os._exit(0)   # threads and child processes must not delay the new runtime


if __name__ == "__main__":
    main()
