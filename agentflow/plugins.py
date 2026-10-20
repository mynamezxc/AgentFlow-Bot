"""Owner-managed accounts; agents receive capabilities, never credentials.

Adapters are application code. Skills/recipes cannot register credential handlers,
set destinations or change grants. HTTP redirects and remote scripts are refused.
"""
import base64
import copy
import http.client
import io
import ipaddress
import json
import importlib.util
import re
import socket
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

from .research import PinnedConnection
from .store import DomainError, now, uid


CATALOG = {
    "gmail": {"name": "Gmail", "docs": "https://developers.google.com/workspace/gmail/api",
        "description": "Read mail through OAuth. Sending mail requires explicit owner permission.",
        "secret_fields": ["access_token", "refresh_token", "client_id", "client_secret"],
        "operations": {"messages.list": {"query": "Gmail search query (optional)", "limit": "1–50"},
                       "messages.get": {"id": "Message ID"},
                       "messages.send": {"to": "Recipient address", "subject": "Subject", "body": "Text content"}},
        "write_operations": ["messages.send"]},
    "postman": {"name": "Postman", "docs": "https://learning.postman.com/api-docs/api-reference/",
        "description": "View workspaces and collection structure. Variable values, authentication and scripts are excluded.",
        "secret_fields": ["token"], "operations": {"workspaces.list": {}, "collections.list": {}, "collections.get": {"id": "ID collection"}},
        "write_operations": []},
    "cloudflare": {"name": "Cloudflare", "docs": "https://developers.cloudflare.com/api/",
        "description": "Read zones and DNS. DNS changes require separate operation permission and an authorized zone.",
        "secret_fields": ["token"], "operations": {"zones.list": {}, "dns.list": {"zone_id": "Authorized zone"},
            "dns.create": {"zone_id": "Authorized zone", "type": "A|AAAA|CNAME|TXT", "name": "DNS name", "content": "Value", "proxied": "true/false"}},
        "write_operations": ["dns.create"]},
    "hugging-face": {"name": "Hugging Face", "docs": "https://huggingface.co/docs/hub/api",
        "description": "Find and inspect model metadata through your connected Hub account.",
        "secret_fields": ["token"], "operations": {"models.search": {"query": "Keywords", "limit": "1–50"}, "models.get": {"id": "organization/model"}},
        "write_operations": []},
    "ssh": {"name": "SSH", "docs": "https://docs.paramiko.org/en/stable/",
        "description": "Run named SSH actions with a verified host key. Installing the app installs the SSH library. Agents do not receive an unrestricted shell.",
        "secret_fields": ["private_key", "passphrase"], "operations": {"actions.run": {"action": "Owner-defined action name"}},
        "write_operations": ["actions.run"]},
    "web": {"name": "Web / API account", "docs": "",
        "description": "Broker fixed API routes and session cookies for owner-configured origins. Does not control a website interface.",
        "secret_fields": ["token", "cookie"], "operations": {"routes.call": {"route": "Owner-defined route name"}},
        "write_operations": ["routes.call"]},
}
PRIVATE_KEYS = re.compile(r"token|secret|password|authorization|cookie|private.?key|api.?key|credential", re.I)


def segment(value, slash=False):
    value = str(value or "")
    if not value or len(value) > 250 or not re.fullmatch(r"[\w.@:-]+(?:/[\w.@:-]+)?" if slash else r"[\w.@:-]+", value):
        raise DomainError("Invalid resource ID.")
    return quote(value, safe="/" if slash else "")


def request_json(host, path, headers=None, body=None, method="GET"):
    """Fixed adapter destinations, DNS pinned and no redirects/proxy inheritance."""
    try:
        addresses = {x[4][0] for x in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(x).is_global for x in addresses):
            raise DomainError("Plugins can connect only to public HTTPS addresses.")
        conn = PinnedConnection(host, sorted(addresses)[0])
        try:
            data = None if body is None else json.dumps(body).encode()
            conn.request(method, path, body=data, headers={"Accept": "application/json", "Content-Type": "application/json", **(headers or {})})
            response = conn.getresponse()
            raw = response.read(1000001)
            if len(raw) > 1000000 or not 200 <= response.status < 300:
                raise DomainError("The plugin returned HTTP " + str(response.status) + "; check account permissions and result size.", 502)
            return json.loads(raw)
        finally:
            conn.close()
    except DomainError:
        raise
    except (OSError, ValueError, http.client.HTTPException):
        raise DomainError("Cannot read the plugin response. Credentials are excluded from errors.", 502) from None


class PluginBroker:
    def __init__(self, engine):
        self.engine, self.store, self.vault = engine, engine.store, engine.vault
        self.lock = threading.RLock()
        self.packages = self.store.directory / "plugin-packages"
        if self.packages.is_dir():
            sys.path.insert(0, str(self.packages))

    def catalog(self):
        installed = self.store.settings().get("installed_plugins", [])
        return [{"id": key, **copy.deepcopy(value), "installed": key in installed} for key, value in CATALOG.items()]

    def install(self, plugin_id, enabled=True):
        if plugin_id not in CATALOG:
            raise DomainError("No runtime adapter is available for this plugin.")
        if plugin_id == "ssh" and enabled and importlib.util.find_spec("paramiko") is None:
            self.install_ssh_dependency()
        with self.store.transaction():
            installed = set(self.store.settings().get("installed_plugins", []))
            installed.add(plugin_id) if enabled else installed.discard(plugin_id)
            self.store.settings({"installed_plugins": sorted(installed)})
            # Removal also revokes standing grants; reinstall never restores access silently.
            if not enabled:
                for account in self.store.all("plugin_accounts"):
                    if account["plugin_id"] == plugin_id:
                        self.store.patch("plugin_accounts", account["id"], {"enabled": False, "verified_at": None})
        return {"installed": enabled, "plugin_id": plugin_id}

    def install_ssh_dependency(self):
        # Only this bundled, owner-installed adapter has a dependency installer.
        # Agent skills and recipes cannot reach this method or choose packages.
        with self.lock:
            self.packages.mkdir(parents=True, exist_ok=True)
            requirement = Path(__file__).resolve().parents[1] / "requirements-ssh.txt"
            try:
                result = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
                    "--target", str(self.packages), "--requirement", str(requirement)],
                    env=self.engine.providers.child_environment(), capture_output=True, timeout=240,
                    creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            except (OSError, subprocess.TimeoutExpired):
                raise DomainError("Could not install the SSH library. Check network access and pip, then install the app again.",502) from None
            if result.returncode:
                raise DomainError("Could not install the SSH library. Check network access and pip, then install the app again.",502)
            if str(self.packages) not in sys.path:
                sys.path.insert(0,str(self.packages))
            importlib.invalidate_caches()

    def accounts(self, actor=None):
        result = []
        installed = self.store.settings().get("installed_plugins", [])
        for item in self.store.all("plugin_accounts"):
            if actor and (not item.get("enabled") or item["plugin_id"] not in installed or
                          not ("*" in item["agent_ids"] or actor["id"] in item["agent_ids"])):
                continue
            result.append({k: item.get(k) for k in ("id", "plugin_id", "label", "enabled", "agent_ids", "operations", "version", "verified_at")})
            result[-1]["verified_at"] = self.store.settings().get("plugin_verified_"+item["id"]) if item.get("enabled") else None
        return result

    def owner_state(self):
        # Config never appears in model context; commands/host keys stay owner-only.
        return {"catalog": self.catalog(), "accounts": [{**a, "config": self.store.get("plugin_accounts", a["id"]).get("config", {})}
                for a in self.accounts()]}

    def save_account(self, data):
        with self.lock:
            previous = self.store.get("plugin_accounts", data["id"]) if data.get("id") else None
            plugin_id = data.get("plugin_id", (previous or {}).get("plugin_id"))
            if plugin_id not in self.store.settings().get("installed_plugins", []):
                raise DomainError("Install the plugin before connecting an account.")
            if previous and (data.get("version") != previous["version"] or plugin_id != previous["plugin_id"]):
                raise DomainError("The account has changed. Reload before saving.", 409)
            label = str(data.get("label", "")).strip()
            if not label or len(label) > 120:
                raise DomainError("Connection names must contain 1 to 120 characters.")
            operations, agents = data.get("operations", []), data.get("agent_ids", [])
            if not isinstance(operations, list) or set(operations) - set(CATALOG[plugin_id]["operations"]):
                raise DomainError("Plugin operation not supported.")
            if not isinstance(agents, list) or len(agents) > 128:
                raise DomainError("Choose agents allowed to use this connection.")
            for agent in agents:
                if agent != "*":
                    self.store.get("agents", agent)
            config = self.validate_config(plugin_id, data.get("config", {}))
            secrets = data.get("secrets", {})
            if not isinstance(secrets, dict) or set(secrets) - set(CATALOG[plugin_id]["secret_fields"]):
                raise DomainError("Secret field not supported.")
            secrets = dict(secrets)
            if isinstance(secrets.get("private_key"),str) and secrets["private_key"].startswith("base64:"):
                try:
                    secrets["private_key"] = base64.b64decode(secrets["private_key"][7:],validate=True).decode("utf-8")
                except (ValueError,UnicodeError):
                    raise DomainError("Invalid SSH private key format.") from None
            account_id = previous["id"] if previous else uid("account")
            if any(not isinstance(v, str) or len(v) > 32000 for v in secrets.values()):
                raise DomainError("Invalid credentials.")
            supplied = {self.secret_key(account_id, k): v for k, v in secrets.items() if v}
            # A credential replacement is a complete bundle; never combine the new
            # access token with an old account's refresh token or session cookie.
            persisted = self.vault.set(supplied, replace_prefix="PLUGIN_"+account_id+"_" if supplied else None)
            doc = {"plugin_id": plugin_id, "label": label, "config": config, "agent_ids": agents,
                   "operations": operations, "enabled": bool(data.get("enabled", True)), "verified_at": None}
            with self.store.transaction():
                self.store.settings({"plugin_verified_"+account_id: None})
                if previous:
                    self.store.patch("plugin_accounts", account_id, doc, previous["version"])
                else:
                    self.store.create("plugin_accounts", {"id": account_id, **doc})
            return {**next(a for a in self.accounts() if a["id"] == account_id),
                    **({"notice": persisted["notice"]} if persisted.get("notice") else {})}

    @staticmethod
    def secret_key(account_id, field):
        return "PLUGIN_" + account_id + "_" + field

    def secret(self, account, field):
        return self.vault.get(self.secret_key(account["id"], field))

    def validate_config(self, plugin_id, config):
        if not isinstance(config, dict) or len(json.dumps(config)) > 30000:
            raise DomainError("Plugin settings must be a JSON object up to 30 KB.")
        allowed = {"gmail": set(), "postman": set(), "hugging-face": set(), "cloudflare": {"zone_ids"},
                   "ssh": {"host", "port", "username", "host_key", "actions"}, "web": {"origin", "routes"}}[plugin_id]
        if set(config) - allowed:
            raise DomainError("Invalid plugin configuration fields. Enter credentials in separate secret fields.")
        if plugin_id == "cloudflare":
            zones = config.get("zone_ids", [])
            if not isinstance(zones, list) or any(not re.fullmatch(r"[a-fA-F0-9]{32}", z) for z in zones):
                raise DomainError("zone_ids must be a list of 32-character hexadecimal zone IDs.")
        if plugin_id == "ssh":
            if not re.fullmatch(r"[a-zA-Z0-9.-]+", str(config.get("host", ""))) or not re.fullmatch(r"[\w.-]+", str(config.get("username", ""))):
                raise DomainError("SSH requires a valid host and username.")
            if not isinstance(config.get("port", 22), int) or not 1 <= config.get("port", 22) <= 65535:
                raise DomainError("Invalid SSH port.")
            key = str(config.get("host_key", "")).split()
            if len(key) != 2 or not key[0].startswith(("ssh-", "ecdsa-")):
                raise DomainError("SSH requires an exact host_key such as 'ssh-ed25519 AAAA…'. Unknown keys are never accepted automatically.")
            actions = config.get("actions", {})
            if not isinstance(actions, dict) or not actions or len(actions) > 30 or any(not re.fullmatch(r"[\w-]{1,60}", k) or not isinstance(v, str) or not v or len(v)>3000 for k,v in actions.items()):
                raise DomainError("SSH requires owner-defined named actions with fixed commands.")
        if plugin_id == "web":
            origin = urlparse(str(config.get("origin", "")))
            if origin.scheme != "https" or not origin.hostname or origin.username or origin.password or origin.port not in (None, 443) or origin.path not in ("", "/") or origin.query or origin.fragment:
                raise DomainError("Web connections require a public HTTPS origin without a path or credentials.")
            routes = config.get("routes", {})
            if not isinstance(routes, dict) or not routes or len(routes) > 30:
                raise DomainError("Define named routes with fixed methods and paths.")
            for key, route in routes.items():
                if (not re.fullmatch(r"[\w-]{1,60}", key) or not isinstance(route, dict) or set(route)-{"method","path","body"}
                    or route.get("method", "GET") not in ("GET", "POST", "PUT", "DELETE", "PATCH")
                    or not str(route.get("path", "")).startswith("/") or str(route["path"]).startswith("//")
                    or any(c in str(route["path"]) for c in ("\r", "\n", "#"))):
                    raise DomainError("Invalid web route.")
        return config

    def revoke(self, account_id, version):
        with self.lock:
            self.store.patch("plugin_accounts", account_id, {"enabled": False, "verified_at": None}, version)
        return {"revoked": True}

    def clean(self, value):
        if isinstance(value, dict):
            return {k: self.clean(v) for k,v in value.items() if not PRIVATE_KEYS.search(str(k))}
        if isinstance(value, list):
            return [self.clean(v) for v in value[:100]]
        if isinstance(value, str):
            value = self.vault.redact(value)
            # Defensive removal of common secret encodings in reflected API responses.
            for secret in list(self.vault.values.values()) + list(self.vault.redaction_history):
                if len(secret) >= 4:
                    for encoded in (quote(secret, safe=""), base64.b64encode(secret.encode()).decode(), secret.encode().hex()):
                        value = value.replace(encoded, "[secret]")
            return value[:24000]
        return value

    def invoke(self, args, actor, run=None):
        if set(args) - {"account_id", "operation", "arguments"}:
            raise DomainError("Agents may select only accounts, operations and task arguments.", 403)
        with self.lock:
            account = self.store.get("plugin_accounts", args.get("account_id", ""))
            operation, params = args.get("operation"), args.get("arguments", {})
            visible = {a["id"] for a in self.accounts(actor)}
            if account["id"] not in visible or operation not in account["operations"]:
                raise DomainError("The owner has not authorized this agent to use this connection or operation.", 403)
            schema = CATALOG[account["plugin_id"]]["operations"][operation]
            if not isinstance(params, dict) or set(params) - set(schema) or len(json.dumps(params))>20000:
                raise DomainError("Invalid plugin arguments. Agents cannot provide URLs, headers, commands or credentials.")
            revision = account["version"]
        result = self.dispatch(account, operation, params)
        with self.lock:
            current = self.store.get("plugin_accounts", account["id"])
            if current["version"] != revision or account["id"] not in {a["id"] for a in self.accounts(actor)}:
                raise DomainError("The connection changed. Results from the previous account were discarded.", 409)
            self.store.settings({"plugin_verified_"+account["id"]: now()})
        return self.clean(result)

    def bearer(self, account):
        token = self.secret(account, "token")
        if not token:
            raise DomainError("The owner has not connected credentials for this account.", 401)
        return {"Authorization": "Bearer " + token}

    def gmail_token(self, account):
        refresh = self.secret(account, "refresh_token")
        if refresh:
            # OAuth token endpoint expects form encoding, not JSON.
            import urllib.request
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    return None
            data = urlencode({"grant_type": "refresh_token", "refresh_token": refresh,
                "client_id": self.secret(account, "client_id"), "client_secret": self.secret(account, "client_secret")}).encode()
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
                with opener.open(urllib.request.Request("https://oauth2.googleapis.com/token", data=data), timeout=20) as response:
                    token = json.loads(response.read(100000))["access_token"]
                self.vault.set({self.secret_key(account["id"], "access_token"): token})
                return token
            except (OSError, ValueError, KeyError):
                raise DomainError("Could not refresh Gmail OAuth. Reconnect the account in Connections.", 401) from None
        token = self.secret(account, "access_token")
        if not token:
            raise DomainError("Gmail requires an owner-connected access token or OAuth refresh token set.", 401)
        return token

    def dispatch(self, a, operation, p):
        plugin = a["plugin_id"]
        if plugin == "gmail":
            headers = {"Authorization": "Bearer " + self.gmail_token(a)}
            root = "/gmail/v1/users/me/messages"
            if operation == "messages.list":
                return request_json("gmail.googleapis.com", root+"?"+urlencode({"q": str(p.get("query", "")), "maxResults": self.limit(p)}), headers)
            if operation == "messages.get":
                return request_json("gmail.googleapis.com", root+"/"+segment(p.get("id"))+"?format=full", headers)
            if operation == "messages.send":
                from email.message import EmailMessage
                message = EmailMessage()
                try:
                    message["To"], message["Subject"] = str(p["to"]), str(p["subject"])
                    message.set_content(str(p["body"]))
                except (KeyError, ValueError):
                    raise DomainError("Mail requires a valid recipient, subject and body.") from None
                return request_json("gmail.googleapis.com", root+"/send", headers,
                    {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()}, "POST")
        if plugin == "postman":
            token = self.secret(a, "token")
            if not token:
                raise DomainError("The owner has not connected a Postman API key.", 401)
            path = "/workspaces" if operation == "workspaces.list" else "/collections"
            if operation == "collections.get":
                path += "/"+segment(p.get("id"))
            result = request_json("api.getpostman.com", path, {"X-API-Key": token})
            if operation == "collections.get":
                def structural(items):
                    return [{"name": x.get("name"), "method": x.get("request", {}).get("method"),
                             "items": structural(x.get("item", []))} for x in items[:100]]
                collection = result.get("collection", {})
                return {"info": {"name": collection.get("info", {}).get("name")}, "items": structural(collection.get("item", []))}
            return result
        if plugin == "cloudflare":
            headers = self.bearer(a)
            zones = a.get("config", {}).get("zone_ids", [])
            if operation == "zones.list":
                result = request_json("api.cloudflare.com", "/client/v4/zones?per_page=50", headers)
                return {"zones": [{"id": z["id"], "name": z.get("name"), "status": z.get("status")} for z in result.get("result", []) if z["id"] in zones]}
            if p.get("zone_id") not in zones:
                raise DomainError("This zone is not authorized for this connection.", 403)
            path = "/client/v4/zones/"+segment(p["zone_id"])+"/dns_records"
            if operation == "dns.list":
                return request_json("api.cloudflare.com", path+"?per_page=100", headers)
            if p.get("type") not in ("A", "AAAA", "CNAME", "TXT") or not p.get("name") or not p.get("content"):
                raise DomainError("DNS requires a valid type, name and content.")
            return request_json("api.cloudflare.com", path, headers, {"type": p["type"], "name": str(p["name"]), "content": str(p["content"]), "proxied": p.get("proxied") is True, "ttl": 1}, "POST")
        if plugin == "hugging-face":
            path = "/api/models/"+segment(p.get("id"), slash=True) if operation == "models.get" else "/api/models?"+urlencode({"search": str(p.get("query", "")), "limit": self.limit(p)})
            return request_json("huggingface.co", path, self.bearer(a))
        if plugin == "web":
            config = a["config"]
            route = config["routes"].get(p.get("route"))
            if not route:
                raise DomainError("The owner has not defined this route.", 403)
            headers = {}
            if self.secret(a, "token"):
                headers.update(self.bearer(a))
            if self.secret(a, "cookie"):
                headers["Cookie"] = self.secret(a, "cookie")
            return request_json(urlparse(config["origin"]).hostname, route["path"], headers, route.get("body"), route.get("method", "GET"))
        if plugin == "ssh":
            return self.ssh(a, p)
        raise DomainError("This adapter does not support the operation.")

    @staticmethod
    def limit(params):
        try:
            return min(50, max(1, int(params.get("limit", 20))))
        except (ValueError, TypeError):
            raise DomainError("The result limit must be between 1 and 50.") from None

    def ssh(self, account, params):
        try:
            import paramiko
        except ImportError:
            raise DomainError("SSH requires paramiko. Run the runtime Python with 'python -m pip install -r requirements-ssh.txt', then reconnect.") from None
        config = account["config"]
        command = config["actions"].get(params.get("action"))
        if not command:
            raise DomainError("The owner has not defined this SSH action.", 403)
        key_data = self.secret(account, "private_key")
        if not key_data:
            raise DomainError("The owner has not connected an SSH private key.", 401)
        client = paramiko.SSHClient()
        try:
            key_type, key_b64 = config["host_key"].split()
            host_key = paramiko.PKey.from_type_string(key_type, base64.b64decode(key_b64, validate=True))
            host = config["host"] if config.get("port",22)==22 else "["+config["host"]+"]:"+str(config["port"])
            client.get_host_keys().add(host, key_type, host_key)
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            private = None
            for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
                try:
                    private = cls.from_private_key(io.StringIO(key_data), password=self.secret(account,"passphrase") or None)
                    break
                except (ValueError, paramiko.SSHException):
                    continue
            if private is None:
                raise DomainError("Invalid SSH private key or passphrase.")
            client.connect(config["host"], port=config.get("port",22), username=config["username"], pkey=private,
                           allow_agent=False, look_for_keys=False, timeout=15, banner_timeout=15, auth_timeout=15)
            _, stdout, stderr = client.exec_command(command, timeout=60)
            # Bounded channel draining prevents deadlock or unbounded output allocation.
            channel, output, errors = stdout.channel, bytearray(), bytearray()
            import time
            deadline = time.monotonic()+60
            while time.monotonic()<deadline:
                if channel.recv_ready():
                    output.extend(channel.recv(4096))
                if channel.recv_stderr_ready():
                    errors.extend(channel.recv_stderr(4096))
                if len(output)+len(errors)>100000:
                    raise DomainError("SSH output exceeded the limit. The action is not confirmed complete.")
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    return {"exit_code": channel.recv_exit_status(), "stdout": output.decode(errors="replace"), "stderr": errors.decode(errors="replace")}
                time.sleep(.02)
            raise DomainError("SSH action timed out. Check the target state before running it again.")
        except DomainError:
            raise
        except Exception:
            raise DomainError("SSH could not verify the connection or action. Check the host key, account and permissions. New keys are never accepted automatically.", 502) from None
        finally:
            client.close()
