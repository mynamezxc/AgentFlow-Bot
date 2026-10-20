import json
import base64
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlparse

from .store import DomainError, now
from .accounts import Accounts, AccountUnavailable, ProviderLimit, IDENTITY_TTL
from .native import (discover_cli, claude_account, claude_catalog, claude_flags, finish_metadata_process, gemini_acp_catalog,
                     launch_args, GEMINI_ALIASES)
from .models import ModelUnavailable
from .usage import Usage
from .sessions import COMPONENTS, SESSION_ID, NativeSessions, UsageSemantics, codex_turn
from .prompting import PromptBundle, image_key
from .quota import TransientLimit, is_usage_cap, PROBE_DELAYS

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}")
GEMINI_KEY = re.compile(r"[A-Za-z0-9_.-]{8,256}")
_GEMINI_LIMIT = ("429", "resource_exhausted", "quota", "rate limit", "ratelimit", "rate-limit", "too many requests",
                 "exhausted your capacity", "usage limit")
_GEMINI_RESET = re.compile(r"reset(?:s)? after\s+(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+(?:\.\d+)?)s)?", re.IGNORECASE)
_GEMINI_RETRY = re.compile(r"(?:retry(?:\s+in|delay\W+)|try again in)\s*(\d+(?:\.\d+)?)\s*(ms|s)\b", re.IGNORECASE)


def _is_windows():
    return os.name == "nt"


def gemini_retry_seconds(text):
    """Seconds until Gemini says it will accept requests again ("reset after 2h30m", "retry in 41.2s", retryDelay "30s")."""
    reset = _GEMINI_RESET.search(text)
    if reset and any(reset.groups()):
        return int(reset[1] or 0) * 3600 + int(reset[2] or 0) * 60 + float(reset[3] or 0)
    retry = _GEMINI_RETRY.search(text)
    if retry:
        return float(retry[1]) / (1000 if retry[2].lower() == "ms" else 1)
    return None


def http_json(url, body=None, headers=None, timeout=90):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.load(res)
    except urllib.error.HTTPError as exc:
        # Provider bodies may contain credentials; expose only a status code.
        raise DomainError(f"The provider returned HTTP {exc.code}. Check the account, model and quota.", 502) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise DomainError("Cannot connect to the provider. Check the URL and running service.", 502) from None


class Vault:
    """Use Windows DPAPI at rest, or an owner-only local file on other hosts."""
    def __init__(self, directory):
        self.path = Path(directory) / "credentials.bin"
        self.lock = threading.RLock()
        self.redaction_history = set()
        self.values = {}
        self.persistence = "encrypted" if os.name == "nt" else "owner-file"
        if self.path.exists():
            raw = self.path.read_bytes()
            if os.name == "nt":
                raw = self._dpapi(raw, False)
            self.values = json.loads(raw)

    @staticmethod
    def _dpapi(raw, encrypt):
        import ctypes
        from ctypes import wintypes
        class Blob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]
        buffer = ctypes.create_string_buffer(raw)
        source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
        target = Blob()
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        signature = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                     ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        crypt.CryptProtectData.argtypes = signature
        crypt.CryptProtectData.restype = wintypes.BOOL
        crypt.CryptUnprotectData.argtypes = signature
        crypt.CryptUnprotectData.restype = wintypes.BOOL
        if encrypt:
            ok = crypt.CryptProtectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
        else:
            ok = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
        if not ok:
            raise DomainError("Cannot open the credential store for the current Windows account.", 500)
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            release = ctypes.windll.kernel32.LocalFree
            release.argtypes = [ctypes.c_void_p]
            release.restype = ctypes.c_void_p
            release(target.data)

    def get(self, key):
        return self.values.get(key, os.getenv(key, ""))

    def set(self, values, replace_prefix=None):
        with self.lock:
            return self._set(values, replace_prefix)

    def _set(self, values, replace_prefix=None):
        self.redaction_history.update(self.values.values())
        retained = {k:v for k,v in self.values.items() if not replace_prefix or not k.startswith(replace_prefix)}
        merged = {**retained, **{k: str(v) for k, v in values.items() if v}}
        raw = json.dumps(merged).encode()
        if os.name == "nt":
            try:
                raw = self._dpapi(raw, True)
            except DomainError:
                self.values = merged
                self.persistence = "session"
                return {"saved": True, "persistence": "session",
                        "notice": "Windows DPAPI is unavailable. Credentials are stored in memory for this session only. Use .env or run under the primary Windows account for persistent storage."}
        temp = self.path.with_suffix(".tmp")
        temp.write_bytes(raw)
        if os.name != "nt":
            os.chmod(temp, 0o600)
        temp.replace(self.path)
        self.values = merged
        self.persistence = "encrypted" if os.name == "nt" else "owner-file"
        return {"saved": True, "persistence": self.persistence}

    def redact(self, text):
        result = str(text)
        for value in list(self.values.values()) + list(self.redaction_history) + [os.getenv(k, "") for k in (
                "GATEWAY_API_KEY", "FACEBOOK_PAGE_TOKEN", "FACEBOOK_APP_SECRET", "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_WEBHOOK_SECRET", "SEO_WEBHOOK_SECRET")]:
            if value and len(value) >= 4:
                result = result.replace(value, "[secret]")
        return result


class Providers:
    def __init__(self, store, vault, workspace):
        self.store, self.vault, self.workspace = store, vault, Path(workspace)
        self.models = []
        self.catalog_path = store.directory / "model-catalog.json"
        if self.catalog_path.exists():
            try:
                self.models = json.loads(self.catalog_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        self.verified = set()
        self.processes = {}
        self.cli = {name: discover_cli(name)
                    for name in ("codex", "claude", "gemini")}
        self.catalog_errors = {}
        self._gemini_catalog_at = -1e9
        self.accounts = Accounts(self)
        self.account_runs = {}
        self.active_accounts = {}
        self.status_guard = threading.Lock()
        self.status_threads = {}
        self.status_attempts = {}
        self.quota_blocks = {}
        self.quota_path=store.directory/'provider-quota.json'
        try:
            for entry in json.loads(self.quota_path.read_text(encoding='utf-8')):
                block=ProviderLimit(entry['provider'],'')
                block.retry_at=entry.get('retry_at')
                self.quota_blocks[(entry['provider'],entry['fingerprint'])]=block
        except (OSError,ValueError,KeyError,TypeError):pass
        self.usage = Usage(self)
        self.gateway_connected = False
        self.gateway_revision = None
        self.usage.errors = {}
        self.account_errors = {}
        self.identity_failures = {}
        # Probe cadence state (identity / usage / catalog are scheduled separately).
        self.status_plans = {}
        self.last_poll = -1e9
        self._timers = {}
        self._seen_runs = set()
        self._seen_active = {}
        self._watch_lock = threading.Lock()
        self._local = threading.local()

    # Identity is re-confirmed every IDENTITY_TTL seconds (or when credentials change); usage
    # refreshes follow their own cadence so one slow probe never gates the next status call.
    USAGE_ACTIVE = {"codex": 60, "claude": 600, "gemini": 600}   # seconds while the UI is polling status
    USAGE_IDLE = {"codex": 300, "claude": 1200, "gemini": 600}   # seconds when nobody is watching (gemini: identity only)
    USAGE_AFTER_INFERENCE = {"codex": 0, "claude": 120}          # seconds to wait after an inference finishes before reading usage again
    POLL_WINDOW = 45
    USAGE_DEBOUNCE = 20
    IDENTITY_RETRY = 30
    MODELS_RETRY = 120

    def status(self):
        # Browser polling must never wait for a slow native CLI account probe.
        self.last_poll = time.monotonic()
        self._watch_inference()
        self.usage.ensure_watch()
        self.usage.poll_credentials()
        for provider in ("codex", "claude", "gemini", "gateway"):
            self._schedule(provider, polled=True)
        return self._status_rows()

    def background_tick(self):
        """Engine heartbeat hook: keep usage warm when no browser is polling."""
        self._watch_inference()
        self.usage.ensure_watch()
        self.usage.poll_credentials(kick=True)
        if time.monotonic() - self.last_poll < self.POLL_WINDOW:
            return
        for provider in ("codex", "claude", "gemini"):
            self._schedule(provider, polled=False)

    def kick_usage(self, provider):
        """An inference on `provider` just finished: refresh usage soon (debounced)."""
        self._schedule(provider, polled=time.monotonic() - self.last_poll < self.POLL_WINDOW)

    def _watch_inference(self):
        # Detect finished inferences without hooking infer(): active entry gone, or a new account_runs record.
        with self._watch_lock:
            active = {rid: a.get("provider") for rid, a in list(self.active_accounts.items())}
            finished = {p for rid, p in self._seen_active.items() if rid not in active}
            self._seen_active = active
            runs = {rid: v.get("provider") for rid, v in list(self.account_runs.items()) if isinstance(v, dict)}
            finished |= {p for rid, p in runs.items() if rid not in self._seen_runs and p}
            self._seen_runs = set(runs)
        for provider in finished:
            self.usage.note_inference(provider)

    def _arm_timer(self, provider, delay):
        with self._watch_lock:
            if provider in self._timers:
                return
            timer = threading.Timer(max(0.5, delay), self._timer_fired, args=(provider,))
            timer.daemon = True
            self._timers[provider] = timer
        timer.start()

    def _timer_fired(self, provider):
        with self._watch_lock:
            self._timers.pop(provider, None)
        self._schedule(provider, polled=time.monotonic() - self.last_poll < self.POLL_WINDOW)

    def _schedule(self, provider, polled=True):
        if not (self.cli.get(provider) or provider == "gateway" and self.store.settings().get("gateway_url")):
            return False
        revision = self.accounts.revision(provider)
        marks = self.accounts.marks(provider) if provider != "gateway" else revision
        clock = time.monotonic()
        delay = None
        with self.status_guard:
            thread = self.status_threads.get(provider)
            if thread and thread.is_alive():
                if self.usage.refresh_requested.get(provider):
                    self._arm_timer(provider, 2)  # a credential change arrived mid-probe: look again right after
                return False
            attempt = self.status_attempts.setdefault(provider, {})
            plan, delay = self._probe_plan(provider, attempt, revision, marks, clock, polled)
            if plan:
                attempt.update(revision=revision, marks=marks)
                for key in ("identity", "usage", "models"):
                    if plan.get(key):
                        attempt[key + "_at"] = clock
                self.status_plans[provider] = plan
                if plan.get("usage"):
                    self.usage.refresh_requested.pop(provider, None)
                thread = threading.Thread(target=self._probe_status, args=(provider, plan), daemon=True)
                self.status_threads[provider] = thread
                thread.start()
        if not plan and delay:
            self._arm_timer(provider, delay)
        return bool(plan)

    def _probe_plan(self, provider, attempt, revision, marks, clock, polled):
        """What is due now: {'identity','usage','models'} flags (or None) and a delay for a debounced follow-up."""
        if provider == "gateway":
            due = attempt.get("revision") != revision or clock - attempt.get("identity_at", -1e9) >= 30
            return ({"identity": True}, None) if due else (None, None)
        first = "identity_at" not in attempt
        since_identity = clock - attempt.get("identity_at", -1e9)
        marks_changed = attempt.get("marks") not in (None, marks)
        revision_changed = attempt.get("revision") not in (None, revision)
        identity_due = (first or revision_changed or since_identity >= IDENTITY_TTL
                        or marks_changed and since_identity >= self.USAGE_DEBOUNCE)
        since_usage = clock - attempt.get("usage_at", -1e9)
        interval = (self.USAGE_ACTIVE if polled else self.USAGE_IDLE)[provider]
        pending = self.usage.pending_inference.get(provider)
        inference_due = bool(pending and pending > attempt.get("usage_at", -1e9))
        usage_due = (first or revision_changed or since_usage >= interval
                     or marks_changed and since_usage >= self.USAGE_DEBOUNCE
                     or self.usage.has_expired(provider) and since_usage >= self.USAGE_DEBOUNCE)
        delay = None
        if inference_due and provider == "claude" and self.usage.event_fresh(provider):
            self.usage.pending_inference.pop(provider, None)   # the inference's own rate_limit_event already gave fresh numbers
        elif inference_due:
            wait = max(self.USAGE_DEBOUNCE - since_usage, self.USAGE_AFTER_INFERENCE.get(provider, 0) - (clock - pending), 0)
            if wait <= 0:
                usage_due = True
            else:
                delay = wait
        # Claude Code rewrote its credentials: read usage again ~2 s later, without the generic debounce.
        requested = self.usage.refresh_requested.get(provider)
        if requested is not None:
            if clock >= requested:
                usage_due = True
            else:
                delay = min(delay, requested - clock) if delay else requested - clock
        # A failed identity check is retried soon (30 s, doubling to 5 min), not after the full identity TTL.
        if provider in self.account_errors and not identity_due:
            wait = min(IDENTITY_TTL, self.IDENTITY_RETRY * 2 ** max(0, self.identity_failures.get(provider, 1) - 1))
            if since_identity >= wait:
                identity_due = True
            else:
                delay = min(delay, wait - since_identity) if delay else wait - since_identity
        plan = {"identity": identity_due, "usage": usage_due or identity_due and first}
        # The Claude catalog is only fetched when missing, and not more than every MODELS_RETRY seconds.
        plan["models"] = bool(provider in ("claude", "gemini") and (identity_due or usage_due)
                              and clock - attempt.get("models_at", -1e9) >= self.MODELS_RETRY)
        return (plan, None) if plan["identity"] or plan["usage"] else (None, delay)

    def _status_rows(self):
        rows = [{"id": p, "installed": bool(self.cli.get(p)), "configured": bool(self.cli.get(p)) if p != "gateway"
                 else bool(self.store.settings().get("gateway_url")),
                 "connected": False if p in self.account_errors else self.gateway_connected and self.gateway_revision==self.accounts.revision(p) if p == 'gateway' else bool(self.accounts.current.get(p,{}).get('signed_in')) and self.accounts.revisions.get(p)==self.accounts.revision(p),
                 "cli_path": self.cli.get(p),
                 "verified": (p != 'gateway' or self.gateway_connected and self.gateway_revision==self.accounts.revision(p)) and not self._status_pending(p) and any(x.startswith(p + ":") for x in self.verified),
                 "error": self.catalog_errors.get(p, ""),
                 "usage": self.usage.public(p,self._usage_updating(p)) if p in ('codex','claude') else None,
                 "account_label": 'Current account unavailable' if p in self.account_errors else self.accounts.current.get(p, {}).get('label') if self.accounts.revisions.get(p)==self.accounts.revision(p) else 'Checking current account…' if self._status_pending(p) else 'Current account unavailable',
                 "account_changed_at": self.accounts.current.get(p, {}).get("changed_at")}
                for p in ("codex", "claude", "gemini", "gateway")]
        for row in rows:
            if row["id"] == "gemini":
                row.update(self._gemini_status())
        return rows

    def _gemini_status(self):
        """Contract 1: auth mode, whether a Vault/env API key exists, and where the chat catalog came from."""
        state = self.accounts.current.get("gemini") or {}
        current = self.accounts.revisions.get("gemini") == self.accounts.revision("gemini")
        auth = state.get("type") if current and state.get("signed_in") else None
        chat = [m for m in self.models if m["provider"] == "gemini" and "chat" in (m.get("capabilities") or ["chat"])]
        return {"auth": auth if auth in ("oauth", "api_key", "vertex") else None,
                "api_key_set": bool(self.vault.get("GEMINI_API_KEY")),
                "models_source": chat[0].get("source_kind") if chat else None}

    def _status_pending(self, provider):
        # True only while the account identity itself is being re-confirmed.
        thread = self.status_threads.get(provider)
        return bool(thread and thread.is_alive() and self.status_plans.get(provider, {}).get("identity", True))

    def _usage_updating(self, provider):
        thread = self.status_threads.get(provider)
        return bool(thread and thread.is_alive())

    def _probe_status(self, provider, plan=None):
        plan = plan or {"identity": True, "usage": True, "models": True}
        account_confirmed=False
        try:
            # Codex app-server returns the account and the limits in one session: read both with one spawn.
            sink = {} if provider == "codex" and plan.get("identity") and plan.get("usage") else None
            self._local.limits_sink = sink
            try:
                state = self.accounts.check(provider, force=bool(plan.get("identity")))
            finally:
                self._local.limits_sink = None
            account_confirmed=True
            self.account_errors.pop(provider,None)
            self.identity_failures.pop(provider,None)
            if provider == 'gateway':
                revision=self.accounts.revisions.get(provider)
                result=self.gateway('/models')
                if revision!=self.accounts.revision(provider):raise DomainError('Gateway configuration changed during the connection check.',409)
                self.gateway_connected=isinstance(result,dict) and isinstance(result.get('data'),list)
                self.gateway_revision=revision
                if not self.gateway_connected:raise DomainError('Gateway returned an invalid model catalog.',502)
                self.catalog_errors.pop(provider,None)
            if (provider == "claude" and state.get("signed_in") and plan.get("models", True) and not any(m["provider"] == provider for m in self.models)
                    or provider == "gemini" and state.get("signed_in") and plan.get("models", True) and self._gemini_catalog_due()):
                try:
                    self.refresh_models(provider)
                except DomainError:
                    pass  # recorded in catalog_errors; usage and account state stay intact
            if provider in ('codex','claude') and state.get('signed_in') and plan.get("usage", True):
                try:
                    if sink and "result" in sink:
                        updated=self.usage.apply_codex(state,sink["result"])
                    else:
                        updated=getattr(self.usage,'refresh_'+provider)(state,force=True)
                    if updated:self.usage.errors.pop(provider,None)
                except (DomainError,OSError,ValueError) as exc:
                    self.usage.errors[provider]=str(exc) if isinstance(exc,DomainError) else 'Usage refresh failed; last confirmed data is retained.'
        except DomainError as exc:
            if not account_confirmed:
                self.account_errors[provider]=str(exc)
                self.identity_failures[provider]=self.identity_failures.get(provider,0)+1
            if provider == 'gateway':self.gateway_connected=False
            with self.accounts.lock:
                # A slow `claude auth status` while the identity files are unchanged is not a sign-out:
                # keep the verified models and the confirmed account; only the error is shown.
                known = (self.accounts.current.get(provider) or self.accounts.persisted.get(provider) or {}).get('identity_key')
                same_identity = provider == 'claude' and bool(known) and known == self.accounts.claude_identity_key()
                if not same_identity:
                    self.verified = {s for s in self.verified if not s.startswith(provider + ":")}
                    self.models = [m for m in self.models if m["provider"] != provider]
                if self.accounts.revisions.get(provider)!=self.accounts.revision(provider):
                    self.accounts.current.pop(provider, None)
                self.catalog_errors[provider] = str(exc)

    def _gemini_catalog_due(self):
        """No Gemini catalog yet, or only the documented aliases (ACP/API unavailable earlier): retry every 30 minutes."""
        entries = [m for m in self.models if m["provider"] == "gemini"]
        if not entries:
            return True
        return all(m.get("source_kind") == "aliases" for m in entries) and time.monotonic() - self._gemini_catalog_at >= 1800

    def gateway(self, route, payload=None):
        base = self.store.settings().get("gateway_url", "").rstrip("/")
        parsed = urlparse(base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise DomainError("Gateway URL must use HTTP(S) and must not contain a password.")
        key = self.vault.get("GATEWAY_API_KEY")
        return http_json(base + route, payload, {"Authorization": "Bearer " + key} if key else {})

    def refresh_models(self, provider):
        found = []
        try:
            session = self.accounts.check(provider, force=True)
            if provider == "gateway":
                result = self.gateway("/models")
                found = [{"id": str(m["id"]), "name": str(m.get("name") or m["id"]),
                          "provider": provider, "source": "GET /v1/models", "observed_at": now()}
                         for m in result.get("data", []) if m.get("id")]
            elif provider == "codex":
                found = self._codex_models()
            elif provider == "claude":
                if not session.get("signed_in"):
                    raise AccountUnavailable(provider)
                entries = claude_catalog(self.cli[provider], self.child_environment())
                found = [{"id": m["value"], "name": m.get("displayName", m["value"]),
                    "resolved_model": m.get("resolvedModel"), "provider": provider,
                    "source": "Claude Code SDK initialize", "observed_at": now(),
                    "description": m.get("description", ""), "modalities": ["text"],
                    "reasoning_efforts": m.get("supportedEffortLevels", []) if m.get("supportsEffort") else [],
                    "default_reasoning_effort": m.get("defaultEffort")}
                    for m in entries if m.get("value")]
            elif provider == "gemini":
                if not session.get("signed_in"):
                    raise AccountUnavailable(provider)
                found = self._gemini_models(session)
            else:
                raise DomainError("Invalid provider.")
            with self.accounts.lock:
                self.accounts.assert_current(provider, session["fingerprint"])
                found = [{**m, "account_fingerprint": session["fingerprint"]} for m in found]
                self.models = [m for m in self.models if m["provider"] != provider] + found
                temporary = self.catalog_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(self.models, ensure_ascii=False), encoding="utf-8")
                temporary.replace(self.catalog_path)
            self.catalog_errors.pop(provider, None)
            return found
        except DomainError as exc:
            self.catalog_errors[provider] = str(exc)
            raise

    def read_account(self, provider):
        if provider == "codex":
            sink = getattr(self._local, "limits_sink", None)
            if sink is not None:
                result = self._codex_models(include_models=False, read_usage=True)
                sink["result"] = result
                return result.get("account")
            return self._codex_models(include_models=False)
        if provider == "gateway":
            return {"type": "gateway", "label": "Gateway: " + (urlparse(self.store.settings().get("gateway_url", "")).hostname or "not configured")}
        if provider == "claude":
            if not self.cli.get(provider):
                raise DomainError("Claude Code CLI not found.")
            return claude_account(self.cli[provider], self.child_environment())
        if provider == "gemini":
            return self.accounts.gemini_account()
        return {"type": "cli", "label": "Session " + provider + " CLI account; identity is managed by the CLI"}

    def child_environment(self):
        excluded = {"GATEWAY_API_KEY", "FACEBOOK_PAGE_TOKEN", "FACEBOOK_APP_SECRET", "META_VERIFY_TOKEN",
            "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "SEO_WEBHOOK_SECRET",
            "OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN", "GEMINI_API_KEY", "GOOGLE_API_KEY"}
        return {k: v for k, v in os.environ.items() if k not in excluded}

    def gemini_environment(self):
        """Environment for a Gemini CLI child: child_environment() (which strips GEMINI_API_KEY / GOOGLE_API_KEY) plus the
        credential of the active auth mode, passed explicitly and only to gemini."""
        env = self.child_environment()
        account = self.accounts.gemini_account() or {}
        if account.get("type") == "api_key":
            env["GEMINI_API_KEY"] = str(self.vault.get("GEMINI_API_KEY") or "")
            env["GEMINI_DEFAULT_AUTH_TYPE"] = "gemini-api-key"
        elif account.get("type") == "vertex":
            env["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
            env["GEMINI_DEFAULT_AUTH_TYPE"] = "vertex-ai"
            if os.getenv("GOOGLE_API_KEY"):
                env["GOOGLE_API_KEY"] = os.environ["GOOGLE_API_KEY"]
        return env

    def _gemini_models(self, session):
        """Gemini catalog, never invented: ACP session/new model list, else the API list (key set), else the documented
        CLI aliases. With an API key the API's image models are added as capability 'image'."""
        executable = self.cli.get("gemini")
        if not executable:
            raise DomainError("Gemini CLI not found. Install it (npm install -g @google/gemini-cli) and sign in on the runtime machine.")
        mode = session.get("type")
        stamp = now()
        def entry(model_id, name, source, kind, capabilities, description=""):
            return {"id": model_id, "name": name, "provider": "gemini", "source": source, "source_kind": kind, "observed_at": stamp,
                    "description": description, "modalities": ["text", "image"], "capabilities": capabilities,
                    "reasoning_efforts": [], "default_reasoning_effort": None}
        try:
            listed = gemini_acp_catalog(executable, self.gemini_environment(), self.workspace, mode)
        except DomainError:
            listed = []
        chat = [entry(m["id"], m["name"], "Gemini CLI ACP session/new", "acp", ["chat"], m["description"]) for m in listed]
        images = []
        key = str(self.vault.get("GEMINI_API_KEY") or "") if mode == "api_key" else ""
        if key:
            try:
                api_chat, images = self._gemini_api_models(key, entry)
            except DomainError:
                api_chat = []
            chat = chat or api_chat
        if not chat:
            chat = [entry(alias, name + " (alias)", "Gemini CLI aliases", "aliases", ["chat"], description)
                    for alias, name, description in GEMINI_ALIASES]
        self._gemini_catalog_at = time.monotonic()
        return chat + images

    def _gemini_api_models(self, key, entry):
        """GET /v1beta/models (paginated). chat = generateContent without image/embedding/tts/veo in the name;
        image = names containing 'image' (Imagen uses another API and is left out)."""
        raw, token = [], ""
        for _ in range(20):
            page = http_json(GEMINI_API + "/models?pageSize=1000" + ("&pageToken=" + quote(token, safe="") if token else ""),
                             headers={"x-goog-api-key": key}, timeout=30)
            raw += [m for m in page.get("models", []) if isinstance(m, dict) and m.get("name")] if isinstance(page, dict) else []
            token = page.get("nextPageToken") if isinstance(page, dict) else None
            if not token:
                break
        chat, images = [], []
        for m in raw:
            model_id = str(m["name"]).removeprefix("models/")
            low, methods = model_id.lower(), m.get("supportedGenerationMethods") or []
            name, description = str(m.get("displayName") or model_id), str(m.get("description") or "")
            if "image" in low and not low.startswith("imagen"):
                if "generateContent" in methods or not methods:
                    images.append(entry(model_id, name, "Gemini API models.list", "api", ["image"], description))
            elif "generateContent" in methods and not any(w in low for w in ("image", "embedding", "tts", "veo")):
                chat.append(entry(model_id, name, "Gemini API models.list", "api", ["chat"], description))
        return chat, images

    def gemini_login(self):
        """Open a visible console running `gemini` so the owner signs in with Google (Windows); instructions elsewhere."""
        executable = self.cli.get("gemini")
        if not executable:
            raise DomainError("Gemini CLI not found. Install it with: npm install -g @google/gemini-cli", 409)
        if not _is_windows():
            return {"launched": False, "command": executable,
                    "instructions": "Run `gemini` in a terminal on the runtime machine and choose 'Sign in with Google'. This page refreshes when the sign-in completes."}
        target = ["powershell.exe", "-NoExit", "-File", executable] if str(executable).lower().endswith(".ps1") else [executable]
        self._launch_console(["cmd", "/c", "start", "Gemini sign-in", *target])
        return {"launched": True}

    def _launch_console(self, args):
        try:
            subprocess.Popen(args, cwd=self.workspace if Path(self.workspace).is_dir() else None, env=self.child_environment(),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=CREATE_NO_WINDOW)
        except OSError:
            raise DomainError("Cannot open the Gemini sign-in console. Run `gemini` in a terminal instead.", 502) from None

    def set_gemini_key(self, api_key):
        """Store (or with None remove) GEMINI_API_KEY in the Vault, then refresh the catalog. The key is never returned."""
        if api_key is not None:
            api_key = str(api_key).strip() if isinstance(api_key, str) else ""
            if not GEMINI_KEY.fullmatch(api_key):
                raise DomainError("Enter a valid Gemini API key (8-256 letters, digits, dashes or underscores).")
        saved = self.vault.set({"GEMINI_API_KEY": api_key} if api_key else {}, replace_prefix="GEMINI_API_KEY")
        error, count = None, 0
        state = self.accounts.check("gemini", force=True)
        if state.get("signed_in"):
            try:
                count = len(self.refresh_models("gemini"))
            except DomainError as exc:
                error = str(exc)
        else:
            self.catalog_errors.pop("gemini", None)
        return {"saved": True, "api_key_set": bool(self.vault.get("GEMINI_API_KEY")), "auth": state.get("type") if state.get("signed_in") else None,
                "persistence": saved.get("persistence"), "models": count, "error": error,
                **({"notice": saved["notice"]} if saved.get("notice") else {})}

    def codex_auth_args(self):
        result = []
        for key, value in self.accounts.auth_config().items():
            result.extend(["--config", key + "=" + json.dumps(value)])
        return result

    def _codex_models(self, include_models=True, read_usage=False):
        executable = self.cli.get("codex")
        if not executable:
            raise DomainError("Codex CLI not found. Install it and sign in on the runtime machine.")
        proc = subprocess.Popen([executable, "app-server", "--listen", "stdio://",
                                 "--config", 'model_provider="openai"', *self.codex_auth_args()],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, encoding="utf-8", env=self.child_environment(), creationflags=CREATE_NO_WINDOW)
        lines = queue.Queue()
        def read():
            try:
                for line in proc.stdout:
                    try:lines.put(json.loads(line))
                    except ValueError:continue
            finally:
                proc.stdout.close()
                lines.put(None)
        reader=threading.Thread(target=read, daemon=True)
        reader.start()
        def send(message):
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
        def response(request_id):
            import time
            until = time.monotonic() + 20
            while time.monotonic() < until:
                try:
                    message = lines.get(timeout=max(0.1, until - time.monotonic()))
                except queue.Empty:
                    break
                if message is None:
                    raise DomainError("Cannot start Codex app-server. Check Windows permissions, CODEX_HOME and sign-in.", 502)
                if message.get("id") == request_id:
                    if "error" in message:
                        raise DomainError("Codex could not complete the metadata request. Check sign-in and account access.", 502)
                    return message.get("result", {})
            raise DomainError("Timed out reading the Codex model catalog.", 504)
        try:
            send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "agentflow", "title": "AgentFlow", "version": "0.1.0"}}})
            response(1)
            send({"method": "initialized", "params": {}})
            send({"id": 2, "method": "account/read", "params": {"refreshToken": False}})
            account = response(2).get("account")
            if read_usage:
                send({'id':3,'method':'account/rateLimits/read','params':{}})
                try:
                    return {'account':account, 'limits':response(3)}
                except DomainError as exc:
                    # Keep the account read; a failed limits call must not look like an account failure.
                    return {'account':account, 'limits':{}, 'limits_error':str(exc)}
            if not include_models:
                return account
            catalog, cursor, request_id = [], None, 3
            while True:
                send({"id": request_id, "method": "model/list", "params": {"limit": 100, "includeHidden": False, **({"cursor": cursor} if cursor else {})}})
                result = response(request_id)
                catalog.extend(result.get("data", []))
                cursor = result.get("nextCursor")
                if not cursor:
                    break
                request_id += 1
            return [{"id": m.get("model") or m["id"], "name": m.get("displayName", m["id"]),
                     "provider": "codex", "source": "Codex model/list", "observed_at": now(),
                     "modalities": m.get("inputModalities", ["text"]),
                     "reasoning_efforts": [e["reasoningEffort"] for e in m.get("supportedReasoningEfforts", [])],
                     "default_reasoning_effort": m.get("defaultReasoningEffort")}
                    for m in catalog]
        finally:
            finish_metadata_process(proc,reader)

    def infer(self, agent, prompt, run_id):
        self.account_runs.pop(run_id, None)
        provider = agent.get("provider")
        session = self.accounts.check(provider, force=True)
        if provider in ("codex", "claude", "gemini") and not session["signed_in"]:
            raise AccountUnavailable(provider)
        key = (provider, session['fingerprint'])
        block = self.quota_blocks.get(key)
        if block:
            if block.retry_at and block.retry_at > time.time():
                block.planned = True   # a stored block's retry_at is already final
                raise block
            # Expired (or legacy "unknown reset") blocks never gate a new attempt.
            self.quota_blocks.pop(key, None)
            self._save_quota_blocks()
        self.active_accounts[run_id] = {"provider": provider, "fingerprint": session["fingerprint"]}
        try:
            result = self._infer(agent, prompt, run_id, session)
            planner = getattr(self, 'quota', None)
            if planner:
                try:planner.on_success(provider, session['fingerprint'])
                except Exception:pass
            return result
        except ProviderLimit as exc:
            windows=self.usage.public(provider).get('windows',[])
            resets=[w['resets_at'] for w in windows if w.get('resets_at') and not w.get('expired') and (w.get('status')=='rejected' or (w.get('used_percent') or 0)>=100)]
            if resets:exc.retry_at=max(resets+([exc.retry_at] if exc.retry_at else []))
            if not is_usage_cap(exc, exhausted=bool(resets)):
                # A short rate limit/overload is retried with 30s/2m/5m backoff; it is not a quota episode.
                raise TransientLimit(provider, getattr(exc, 'detail', '')) from None
            planner = getattr(self, 'quota', None)
            if planner:planner.plan(provider, session['fingerprint'], exc)
            elif exc.retry_at is None:exc.retry_at = time.time() + PROBE_DELAYS[0]
            self.quota_blocks[key] = exc
            self._save_quota_blocks()
            raise
        finally:
            self.active_accounts.pop(run_id, None)

    # Guards quota_blocks and provider-quota.json (never nested inside accounts.lock or the store lock).
    _quota_lock = threading.Lock()

    def _save_quota_blocks(self):
        with self._quota_lock:
            tmp=self.quota_path.with_suffix('.tmp')
            tmp.write_text(json.dumps([{'provider':p,'fingerprint':f,'retry_at':b.retry_at} for (p,f),b in list(self.quota_blocks.items())[-8:]]),encoding='utf-8')
            tmp.replace(self.quota_path)

    @property
    def sessions(self):
        """Registry of native CLI sessions AgentFlow created (lazy; see sessions.NativeSessions)."""
        if getattr(self, "_sessions", None) is None:
            self._sessions = NativeSessions(self)
        return self._sessions

    @property
    def usage_semantics(self):
        """Persisted per-provider usage semantics (see sessions.UsageSemantics); lazy."""
        if getattr(self, "_usage_semantics", None) is None:
            self._usage_semantics = UsageSemantics(self.store.directory)
        return self._usage_semantics

    def _native_state(self):
        return self.__dict__.setdefault("_native", {"strikes": {}, "disabled": set()})

    def _prompt_file(self, bundle):
        """Content-addressed copy of the stable prefix (Codex model_instructions_file / long Claude prompts)."""
        directory = Path(self.store.directory) / "prompts"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (bundle.system_digest + ".md")
        if path.exists():
            try:
                os.utime(path)
            except OSError:
                pass
            return path
        temporary = directory / f"{bundle.system_digest}.{threading.get_ident()}.tmp"
        temporary.write_text(bundle.system, encoding="utf-8")
        temporary.replace(path)
        files = sorted((p for p in directory.glob("*.md") if p != path), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[49:]:
            try:
                old.unlink()
            except OSError:
                pass
        return path

    @staticmethod
    def _usage_numbers(provider, raw):
        """Provider usage -> {input_tokens (uncached), cached_input_tokens, cache_creation_tokens, output_tokens}."""
        if not isinstance(raw, dict):
            return None
        def number(key):
            value = raw.get(key)
            return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
        if provider == "claude":
            return {"input_tokens": number("input_tokens"), "cached_input_tokens": number("cache_read_input_tokens"),
                    "cache_creation_tokens": number("cache_creation_input_tokens"), "output_tokens": number("output_tokens")}
        cached = number("cached_input_tokens")
        # Codex input_tokens includes the cached portion.
        return {"input_tokens": max(0, number("input_tokens") - cached), "cached_input_tokens": cached,
                "cache_creation_tokens": 0, "output_tokens": number("output_tokens")}

    def _spawn(self, args, stdin, run_id, env=None, shim=False):
        # shim=True: npm .cmd launchers go through cmd.exe /d /s /c (Gemini only; see native.launch_args).
        proc = subprocess.Popen(launch_args(args[0], args[1:]) if shim else args, cwd=self.workspace, env=env or self.child_environment(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                creationflags=CREATE_NO_WINDOW)
        self.processes[run_id] = proc
        try:
            stdout, stderr = proc.communicate(stdin, timeout=300)
        except subprocess.TimeoutExpired:
            self.cancel(run_id)
            proc.communicate()
            raise DomainError("The agent exceeded the 5-minute timeout. Check the runtime before retrying.", 504) from None
        finally:
            self.processes.pop(run_id, None)
        return stdout, stderr, proc.returncode

    def _cli_command(self, provider, executable, model, effort, bundle, text, pictures, persist, resume_id, session_id):
        """argv and stdin for one Codex/Claude call. `bundle` None keeps the legacy full-prompt format."""
        if provider == "codex":
            instructions = self._prompt_file(bundle) if bundle else Path(__file__).with_name("worker-instructions.md")
            flags = ["--skip-git-repo-check", *([] if persist else ["--ephemeral"]), "--sandbox", "read-only",
                     "--ignore-user-config", "--config", 'web_search="disabled"',
                     "--config", "model_instructions_file=" + json.dumps(str(instructions)),
                     "--disable", "shell_tool", "--disable", "unified_exec", "--disable", "apps",
                     "--disable", "plugins", "--disable", "browser_use", "--disable", "computer_use",
                     "--disable", "image_generation", "--disable", "multi_agent", "--disable", "code_mode_host",
                     "--enable", "skip_host_skill_discovery", "--json", "--model", model]
            head = []
            for picture in pictures:
                head += ["--image", picture["path"]]
            if effort:
                head += ["--config", "model_reasoning_effort=" + json.dumps(effort)]
            head += self.codex_auth_args()
            # Global flags stay before the subcommand: `codex exec <flags> resume <thread_id> -`.
            return [executable, "exec", *head, *flags, *(["resume", resume_id] if resume_id else []), "-"], text
        if provider == "gemini":
            # Prompt on stdin (-p "" makes stdin the prompt); system prompt via GEMINI_SYSTEM_MD (see _gemini_env). Tools are
            # denied by policy, extensions and MCP are switched off, so the model only emits the AgentFlow JSON envelope.
            args = [executable, "-p", "", "-o", "stream-json", "-m", model, "--approval-mode", "default",
                    "--policy", str(Path(__file__).with_name("gemini-policy.toml")), "-e", "none",
                    "--allowed-mcp-server-names", "agentflow-no-mcp"]
            if resume_id:
                args += ["-r", resume_id]
            references, directories = [], []
            for picture in pictures:
                path = Path(picture["path"]).resolve()
                if str(path.parent) not in directories:
                    directories.append(str(path.parent))
                references.append("@" + path.as_posix().replace(" ", "\\ "))
            for directory in directories:
                args += ["--include-directories", directory]
            if references:
                text = text.rstrip("\n") + "\n\nAttached images (read them with the @ references):\n" + "\n".join(references) + "\n"
            return args, text
        system = bundle.system if bundle else Path(__file__).with_name("worker-instructions.md").read_text(encoding="utf-8")
        args = [executable, "-p", "--output-format", "stream-json", "--verbose", "--model", model, *claude_flags(persist)]
        if len(system) > (16000 if os.name == "nt" else 100000) and bundle:
            # Windows limits the whole command line to ~32k characters.
            args += ["--system-prompt-file", str(self._prompt_file(bundle))]
        else:
            args += ["--system-prompt", system]
        if effort:
            args.extend(["--effort", effort])
        if persist:
            args += ["--resume", resume_id] if resume_id else ["--session-id", session_id]
        if pictures:
            args.extend(["--input-format", "stream-json"])
            text = json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}] + [
                {"type": "image", "source": {"type": "base64", "media_type": i["mime"], "data": base64.b64encode(Path(i["path"]).read_bytes()).decode()}} for i in pictures]}}) + "\n"
        return args, text

    def _gemini_env(self, bundle):
        env = self.gemini_environment()
        env["GEMINI_SYSTEM_MD"] = str(self._prompt_file(bundle) if bundle else Path(__file__).with_name("worker-instructions.md"))
        return env

    @staticmethod
    def _gemini_usage(stats):
        """Gemini result stats -> ({input_tokens (uncached), cached_input_tokens, cache_creation_tokens, output_tokens}, [models]).

        stream-json: models.<m> = {total_tokens, input_tokens (prompt, includes cached), output_tokens, cached, input (uncached)};
        json: models.<m>.tokens = {prompt, candidates, total, cached, thoughts, tool}. Per-model figures are summed (an `auto`
        turn can touch a router model too); uncached input = prompt - cached; reasoning tokens not broken out count as output."""
        if not isinstance(stats, dict):
            return None, None
        models = stats.get("models") if isinstance(stats.get("models"), dict) else {}
        blocks = [b for b in models.values() if isinstance(b, dict)] or [stats]
        def num(block, *keys):
            for key in keys:
                value = block.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return int(value)
            return None
        usage, found = {"input_tokens": 0, "cached_input_tokens": 0, "cache_creation_tokens": 0, "output_tokens": 0}, False
        for block in blocks:
            tokens = block.get("tokens") if isinstance(block.get("tokens"), dict) else block
            prompt, cached, fresh = num(tokens, "prompt", "input_tokens"), num(tokens, "cached", "cached_input_tokens") or 0, num(tokens, "input")
            output = num(tokens, "candidates", "output_tokens")
            if prompt is None and fresh is not None:
                prompt = fresh + cached
            if prompt is None and output is None:
                continue
            prompt, output = prompt or 0, (output or 0) + (num(tokens, "thoughts") or 0)
            total = num(tokens, "total", "total_tokens")
            if total and total - prompt > output:
                output = total - prompt
            cached = min(cached, prompt)
            usage["input_tokens"] += prompt - cached
            usage["cached_input_tokens"] += cached
            usage["output_tokens"] += output
            found = True
        return (usage if found else None), (list(models) or None)

    def _gemini_limit(self, provider, text):
        """ProviderLimit for a Gemini 429/quota error. A reset above 2 min becomes retry_at (a usage cap); a short retry
        delay is phrased "try again in Ns" so the quota planner treats it as a transient rate limit."""
        text = self.vault.redact(text)
        seconds = gemini_retry_seconds(text)
        error = ProviderLimit(provider, text + (" (try again in %gs)" % round(seconds, 1) if seconds is not None and seconds <= 120 else ""))
        if seconds is not None and seconds > 120:
            error.retry_at = time.time() + seconds
        return error

    def _gemini_result(self, stdout, stderr, code, session, soft):
        """Parse `gemini -o stream-json` (init -> message* -> tool_* -> error -> result); a single `-o json` document also works."""
        provider = "gemini"
        parsed = {"output": "", "usage": None, "session_id": None, "failed": "", "actual_models": None}
        def fail(message, status=502):
            if soft:
                parsed["failed"] = message
                return parsed
            raise DomainError(message, status)
        segments, errors, result, whole = [[]], [], None, None
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "init":
                found = event.get("session_id") or event.get("sessionId")
                if not parsed["session_id"] and isinstance(found, str) and SESSION_ID.match(found):
                    parsed["session_id"] = found  # later passed on a command line: only well-formed ids are accepted
            elif kind == "message" and event.get("role", "assistant") == "assistant":
                segments[-1].append(str(event.get("content") or ""))
            elif kind in ("tool_use", "tool_result") and segments[-1]:
                segments.append([])  # text before a (denied) tool call is commentary; the answer follows the last one
            elif kind == "error" and str(event.get("severity", "error")).lower() != "warning":
                errors.append(str(event.get("message") or event.get("error") or json.dumps(event)))
            elif kind == "result":
                result = event
            elif kind is None and ("response" in event or "stats" in event or "error" in event):
                whole = event
        if result is None and whole is not None:
            result = whole
            segments = [[str(whole.get("response") or "")]]
        if isinstance(result, dict):
            problem = result.get("error")
            if isinstance(problem, dict):
                problem = problem.get("message") or json.dumps(problem)
            if problem:
                errors.append(str(problem))
            elif result.get("status") == "error":
                errors.append("The CLI reported an inference error.")
            parsed["usage"], parsed["actual_models"] = self._gemini_usage(result.get("stats"))
        detail = " ".join(errors + ([stderr] if code and stderr else []))
        if any(word in detail.casefold() for word in _GEMINI_LIMIT):
            self.accounts.assert_current(provider, session["fingerprint"])
            raise self._gemini_limit(provider, detail)
        if code == 41:
            return fail("Gemini CLI is not signed in or its credentials were rejected. Sign in from Connections.", 401)
        if code:
            self.accounts.assert_current(provider, session["fingerprint"])
            return fail(f"gemini CLI exited with code {code}. Check sign-in, quota and CLI version.")
        if errors:
            return fail("The CLI reported an inference error. Check the account and model.")
        parsed["output"] = "".join(next((s for s in reversed(segments) if "".join(s).strip()), []))
        if not parsed["output"].strip():
            return fail("The model returned no content. This task was not marked successful.")
        return parsed

    def _cli_result(self, provider, stdout, stderr, code, session, soft=False):
        """Parse one native CLI call. soft=True (resume attempts) reports failure instead of raising."""
        if provider == "gemini":
            return self._gemini_result(stdout, stderr, code, session, soft)
        parsed = {"output": "", "usage": None, "session_id": None, "failed": "", "actual_models": None}
        def fail(message, status=502):
            if soft:
                parsed["failed"] = message
                return parsed
            raise DomainError(message, status)
        if provider == "claude":
            records = []
            for line in stdout.splitlines():
                try:
                    record = json.loads(line)
                    if isinstance(record, dict):
                        records.append(record)
                except ValueError:
                    continue
            self.usage.claude_events(records, session)
            result = next((r for r in reversed(records) if r.get("type") == "result"), records[-1] if records else {})
            detail = str(result.get("result", ""))
            if result.get("api_error_status") == 429 or result.get("is_error") and "hit" in detail.casefold() and "limit" in detail.casefold():
                self.accounts.assert_current(provider, session["fingerprint"])
                raise ProviderLimit(provider, detail)
            parsed["usage"] = self._usage_numbers(provider, result.get("usage"))
            parsed["session_id"] = result.get("session_id") or next((r.get("session_id") for r in records if r.get("session_id")), None)
            if code:
                self.accounts.assert_current(provider, session["fingerprint"])
                return fail(f"{provider} CLI exited with code {code}. Check sign-in, quota and CLI version.")
            if result.get("is_error") or result.get("error"):
                return fail("The CLI reported an inference error. Check the account and model.")
            parsed["output"] = result.get("result") or result.get("response", "")
            parsed["actual_models"] = list(result.get("modelUsage", {}))
        else:
            events, parts = [], []
            for line in stdout.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                events.append(event)
                detail = json.dumps(event).casefold()
                if event.get("type") in ("error", "turn.failed") and any(t in detail for t in ("usage limit", "rate limit", "quota exceeded", "limit reached")):
                    self.accounts.assert_current(provider, session["fingerprint"])
                    raise ProviderLimit(provider, detail)
                if event.get("type") == "thread.started" and event.get("thread_id"):
                    parsed["session_id"] = event["thread_id"]
                if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                    # Cumulative for the thread (total_token_usage): the LAST report is the thread total; the
                    # per-turn figure is derived in _native_infer. Without a baseline it is the per-turn figure itself.
                    parsed["usage_raw"] = {k: int(event["usage"].get(k, 0) or 0) if isinstance(event["usage"].get(k, 0), (int, float)) else 0
                                           for k in COMPONENTS}
                    parsed["usage"] = self._usage_numbers(provider, event["usage"])
                if event.get("type") == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                    parts.append(event["item"].get("text", ""))
            if code:
                self.accounts.assert_current(provider, session["fingerprint"])
                return fail(f"{provider} CLI exited with code {code}. Check sign-in, quota and CLI version.")
            # Intermediate commentary is not the final structured action envelope.
            parsed["output"] = parts[-1] if parts else ""
        if not isinstance(parsed["output"], str) or not parsed["output"].strip():
            return fail("The model returned no content. This task was not marked successful.")
        return parsed

    def _native_infer(self, provider, executable, prompt, run_id, session, model, effort, images):
        """Codex/Claude call, continuing the run's native session when the prompt bundle allows it."""
        bundle = prompt if isinstance(prompt, PromptBundle) and prompt.system is not None else None
        state = self._native_state()
        persist = bool(bundle and bundle.native and provider not in state["disabled"])
        resume_id = bundle.resume_id if persist and bundle.delta is not None else None
        parsed, resumed, reset = None, False, False
        for resume in ([resume_id] if resume_id else []) + [None]:
            text = (bundle.delta if resume else bundle.volatile) if bundle else prompt
            pictures = [i for i in images if not (bundle and resume and image_key(i) in bundle.sent_images)]
            session_id = resume or (str(uuid.uuid4()) if persist and provider == "claude" else None)
            args, stdin = self._cli_command(provider, executable, model, effort, bundle, text, pictures, persist, resume, session_id)
            if persist and provider == "claude" and not resume:
                self.sessions.register(run_id, provider, session_id)
            stdout, stderr, code = (self._spawn(args, stdin, run_id, env=self._gemini_env(bundle), shim=True) if provider == "gemini"
                                    else self._spawn(args, stdin, run_id))
            parsed = self._cli_result(provider, stdout, stderr, code, session, soft=bool(resume))
            if resume and parsed["failed"]:
                reset, parsed = True, None
                continue
            resumed = bool(resume)
            break
        if persist:
            state["strikes"][provider] = 0 if resumed else state["strikes"].get(provider, 0) + (1 if reset else 0)
            if state["strikes"][provider] >= 3:
                # The installed CLI does not honour resume; stop wasting a spawn per turn.
                state["disabled"].add(provider)
        sid = (resume if resumed else None) or parsed["session_id"] or (session_id if provider == "claude" else None)
        if sid and not resumed and (provider == "gemini" or persist and provider == "codex"):
            # Gemini CLI always persists its chats, so every new session is registered for later pruning.
            self.sessions.register(run_id, provider, sid)
        usage, seen = parsed["usage"], None
        if provider == "codex" and parsed.get("usage_raw") is not None:
            # `codex exec resume` keeps accumulating: bill the difference to the previous report of this thread.
            turn, seen = codex_turn(parsed["usage_raw"], bundle.usage_seen if bundle and resumed else None,
                                    bundle.prior_turns if bundle and resumed else 0, self.usage_semantics)
            usage = self._usage_numbers(provider, turn)
        extra = {"usage": usage}
        if seen is not None:
            extra["usage_seen"] = seen
        if parsed["actual_models"] is not None:
            extra["actual_models"] = parsed["actual_models"]
        if bundle:
            extra["native"] = {"resumed": resumed, "reset": reset, "session_id": sid if persist else None,
                               "sent_chars": len(text) + (0 if resumed else len(bundle.system)),
                               "image_keys": [image_key(i) for i in pictures]}
        self.account_runs[run_id] = {**self.account_runs.get(run_id, {}), **extra}
        return parsed["output"]

    def _infer(self, agent, prompt, run_id, session):
        provider, model = agent.get("provider"), agent.get("model", "")
        images=agent.get("input_images",[])
        if provider == "gemini" and model and not GEMINI_MODEL_ID.fullmatch(model):
            raise ModelUnavailable(model)
        if provider in ("codex", "claude", "gemini", "gateway") and not any(m["provider"] == provider for m in self.models):
            self.refresh_models(provider)
        if not model:
            raise DomainError(f"No model selected for {agent['name']}. Open the agent profile to configure it.")
        if provider in ("codex", "claude", "gemini", "gateway") and not any(
                m["provider"] == provider and model in (m["id"], m.get("resolved_model")) and "chat" in (m.get("capabilities") or ["chat"]) for m in self.models):
            raise ModelUnavailable(model)
        effort = agent.get("reasoning_effort")
        entry = next((m for m in self.models if m["provider"] == provider and model in (m["id"], m.get("resolved_model"))), {})
        if effort and effort not in entry.get("reasoning_efforts", []):
            raise DomainError("This reasoning level is not supported by the current model. Choose another level in chat.")
        if provider == "gateway":
            content=prompt if not images else [{"type":"text","text":prompt}]+[{"type":"image_url","image_url":{"url":"data:"+i["mime"]+";base64,"+base64.b64encode(Path(i["path"]).read_bytes()).decode()}} for i in images]
            result = self.gateway("/chat/completions", {"model": model, "messages": [{"role": "user", "content": content}],
                                                       "stream": False, **({"reasoning_effort": effort} if effort else {})})
            try:
                output = result["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise DomainError("The gateway returned an invalid response.", 502) from None
        else:
            executable = self.cli.get(provider)
            if not executable:
                raise DomainError(f"Missing {provider} CLI on the runtime machine.")
            if provider in ("codex", "claude", "gemini"):
                output = self._native_infer(provider, executable, prompt, run_id, session, model, effort, images)
            else:
                raise DomainError("Provider not supported.")
        if not isinstance(output, str) or not output.strip():
            raise DomainError("The model returned no content. This task was not marked successful.", 502)
        with self.accounts.lock:
            self.accounts.assert_current(provider, session["fingerprint"])
            self.account_runs[run_id] = {**self.account_runs.get(run_id, {}), "provider": provider, "fingerprint": session["fingerprint"]}
            self.verified.add(provider + ":" + model)
        return self.vault.redact(output)

    def cancel(self, run_id):
        process = self.processes.get(run_id)
        if process:
            try:
                if os.name=='nt' and isinstance(process.pid,int):
                    subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                        creationflags=CREATE_NO_WINDOW,timeout=10)
                else:process.kill()
            except (OSError,subprocess.TimeoutExpired):
                try:process.kill()
                except OSError:pass
