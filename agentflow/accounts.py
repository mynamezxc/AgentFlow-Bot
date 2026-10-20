import hashlib
import json
import os
import threading
import time
import tomllib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .store import DomainError, now

# Identity (who is signed in) is re-confirmed at most this often unless a credential
# file changes; usage windows are bound to that identity, never to file timestamps.
IDENTITY_TTL = 300
_UNSET = object()
_VOLATILE = re.compile(r"token|secret|expir|plan|refresh|credit|balance|usage|limit|_at$|At$|time|last|checked|updated|^iat$|^exp$|^nonce", re.IGNORECASE)


def stable_identity(account):
    """Identity-only view of an account dict: drops plan, tokens, expiries and timestamps."""
    if isinstance(account, dict):
        return {k: stable_identity(v) for k, v in account.items() if not _VOLATILE.search(str(k))}
    if isinstance(account, list):
        return [stable_identity(v) for v in account]
    return account


def identity_digest(account):
    return hashlib.sha256(json.dumps(stable_identity(account), sort_keys=True, default=str).encode()).hexdigest()


class AccountChanged(DomainError):
    def __init__(self):
        super().__init__("The account changed during execution. Results from the previous account are discarded; work continues with the current account.", 503)


class AccountUnavailable(DomainError):
    def __init__(self, provider):
        super().__init__("Not signed in to " + provider + ". Sign in through the CLI so work can resume.", 401)


class ProviderLimit(DomainError):
    def __init__(self, provider, detail):
        self.retry_at = None
        self.detail = str(detail)[:2000]
        reset = re.search(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*\((Asia/Ho_Chi_Minh|Asia/Saigon)\)", detail, re.IGNORECASE)
        notice = "No confirmed reset time; automatic retries are paused."
        if reset:
            hour, minute = int(reset[1]), int(reset[2] or 0)
            if reset[3]:
                hour = hour % 12 + (12 if reset[3].lower() == "pm" else 0)
            if 0 <= hour < 24 and 0 <= minute < 60:
                current = datetime.now(timezone(timedelta(hours=7)))
                target = current.replace(hour=hour, minute=minute, second=5, microsecond=0)
                if target <= current:
                    target += timedelta(days=1)
                self.retry_at = target.timestamp()
                notice = "The provider reports a reset at " + target.strftime("%H:%M %d/%m") + " (Vietnam time)."
        super().__init__("Account " + provider + " has reached its session limit. " + notice +
            " Work retains its checkpoint. Choose another model or provider in chat if needed.", 429)


class Accounts:
    """Cache identity metadata only; native CLIs remain the credential owners."""
    def __init__(self, providers):
        self.providers = providers
        self.path = providers.store.directory / "provider-accounts.json"
        self.lock = threading.RLock()
        self.current = {}
        self.checked = {}
        self.revisions = {}
        self.persisted = {}
        self.refreshing = set()
        if self.path.is_file():
            try:
                self.persisted = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        self._identity_cache = {}
        # Reuse a verified, account-bound snapshot on restart only when the local
        # credential identity still matches. Native probes still run in background,
        # so persisted usage windows show immediately for the same identity.
        for provider in ("codex", "claude", "gemini"):
            previous = self.persisted.get(provider, {})
            if self.has_identity(provider) and previous.get("fingerprint") and previous.get("identity_revision") == self.revision(provider):
                self.current[provider] = previous
                self.revisions[provider] = previous["identity_revision"]

    def home(self, provider):
        home = Path(os.getenv("USERPROFILE") or Path.home())
        if provider == "gemini":
            # GEMINI_CLI_HOME is the directory that CONTAINS .gemini (it replaces the user's home directory).
            return Path(os.getenv("GEMINI_CLI_HOME") or home) / ".gemini"
        return Path(os.getenv({"codex": "CODEX_HOME", "claude": "CLAUDE_CONFIG_DIR"}[provider])
                    or home / {"codex": ".codex", "claude": ".claude"}[provider])

    def auth_config(self):
        try:
            config = tomllib.loads((self.home("codex") / "config.toml").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: config[k] for k in ("cli_auth_credentials_store", "forced_login_method",
            "forced_chatgpt_workspace_id", "chatgpt_base_url") if k in config}

    def revision(self, provider):
        if provider == "gateway":
            return hashlib.sha256((self.providers.store.settings().get("gateway_url", "") + "\0"
                                   + self.providers.vault.get("GATEWAY_API_KEY")).encode()).hexdigest()
        root = self.home(provider)
        if provider == 'codex':
            # The extension updates config.toml for models/effort and refreshes
            # OAuth tokens. Neither operation changes the signed-in identity.
            # File mtimes therefore must not invalidate quota observations.
            try:
                auth=json.loads((root/'auth.json').read_text(encoding='utf-8'))
                tokens=auth.get('tokens') or {}
                identity=self.scope_hint()
                material={'home':str(root),'config':self.auth_config(),'identity':identity,
                    'auth_mode':auth.get('auth_mode'),
                    'has_credential':bool(tokens.get('access_token') or tokens.get('refresh_token') or auth.get('OPENAI_API_KEY'))}
            except (OSError,ValueError,AttributeError,TypeError):
                material={'home':str(root),'config':self.auth_config(),'identity':'native-store-unavailable'}
            return hashlib.sha256(json.dumps(material,sort_keys=True).encode()).hexdigest()
        if provider == "claude":
            # Claude Code rewrites .credentials.json on every OAuth refresh (often during our
            # own inference). That is not an identity change, so only stable identity counts.
            credentials = self.claude_credentials()
            material = {"home": str(root), "identity": self.claude_identity(), "present": credentials["present"],
                        "subscription": credentials["subscription"], "tier": credentials["tier"]}
            return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
        if provider == "gemini":
            # Identity only: Gemini CLI rewrites oauth_creds.json on every token refresh, which is not a re-login.
            account = self.gemini_account()
            material = {"home": str(root), "account": stable_identity(account) if account else None}
            return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
        return self.marks(provider)

    def marks(self, provider):
        """File mtime/size hash. Only a hint that a background re-check is worthwhile."""
        root = self.home(provider)
        names = {"codex": ("auth.json", "config.toml"), "claude": (".credentials.json", "settings.json"),
                 "gemini": ("oauth_creds.json", "google_accounts.json", "settings.json")}.get(provider, ())
        marks = [str(root)]
        for name in names:
            try:
                info = (root / name).stat()
                marks.append((name, info.st_mtime_ns, info.st_size))
            except OSError:
                marks.append((name, None))
        return hashlib.sha256(json.dumps(marks).encode()).hexdigest()

    def claude_credentials(self):
        """Non-secret facts about the Claude OAuth store; tokens are never returned.

        Claude Code rewrites this file on every OAuth refresh; a read that lands mid-write returns the last
        good facts instead of pretending the credentials vanished (that would look like a sign-out)."""
        absent = {"present": False, "subscription": None, "tier": None, "expires_at": None}
        try:
            oauth = (json.loads((self.home("claude") / ".credentials.json").read_text(encoding="utf-8")).get("claudeAiOauth") or {})
            facts = {"present": bool(oauth.get("accessToken") or oauth.get("refreshToken")),
                     "subscription": oauth.get("subscriptionType"), "tier": oauth.get("rateLimitTier"),
                     "expires_at": oauth.get("expiresAt") if isinstance(oauth.get("expiresAt"), (int, float)) else None}
            self._credential_facts = facts
            return facts
        except FileNotFoundError:
            self._credential_facts = None
            return absent
        except (OSError, ValueError, AttributeError, TypeError):
            return getattr(self, "_credential_facts", None) or absent

    def claude_identity(self):
        """Stable Claude account ids from the CLI's own state file (cached per mtime)."""
        root = self.home("claude")
        paths = [root / ".claude.json"]
        if root.name == ".claude":
            paths.insert(0, root.parent / ".claude.json")
        for path in paths:
            try:
                info = path.stat()
            except OSError:
                continue
            key = (info.st_mtime_ns, info.st_size)
            cached = self._identity_cache.get(str(path))
            if cached and (cached[0] == key or info.st_size > 1_000_000 and time.monotonic() - cached[2] < 10):
                # Large state files are rewritten constantly while Claude Code works: re-parse at most every 10 s.
                identity = cached[1]
            else:
                try:
                    account = json.loads(path.read_text(encoding="utf-8")).get("oauthAccount") or {}
                    identity = {k: account[k] for k in ("accountUuid", "organizationUuid", "emailAddress") if account.get(k)}
                    self._identity_cache[str(path)] = (key, identity, time.monotonic())
                except (OSError, ValueError, AttributeError, TypeError):
                    # The CLI rewrites this large file constantly; a torn read keeps the last known identity.
                    identity = cached[1] if cached else {}
            if identity:
                return identity
        return {}

    def claude_identity_key(self):
        """Stable digest of the signed-in Claude account ids (None when unreadable); ignores plan and token fields."""
        identity = self.claude_identity()
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest() if identity else None

    def gemini_oauth(self):
        """Non-secret facts about the Gemini CLI Google sign-in; tokens are only ever hashed, never returned.

        Reads google_accounts.json (active account email) and oauth_creds.json (presence, id_token subject, a digest
        of the refresh token as a last-resort identity). A torn read during a CLI rewrite keeps the last good facts."""
        root = self.home("gemini")
        facts = {"present": False, "email": None, "subject": None, "identity_ref": None}
        def load(name):
            path = root / name
            try:
                info = path.stat()
            except OSError:
                self._identity_cache.pop(str(path), None)
                return None
            key = (info.st_mtime_ns, info.st_size)
            cached = self._identity_cache.get(str(path))
            if cached and cached[0] == key:
                return cached[1]
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return cached[1] if cached else None
            data = data if isinstance(data, dict) else None
            self._identity_cache[str(path)] = (key, data, time.monotonic())
            return data
        accounts = load("google_accounts.json") or {}
        if isinstance(accounts.get("active"), str) and accounts["active"].strip():
            facts["email"] = accounts["active"].strip()
        creds = load("oauth_creds.json") or {}
        facts["present"] = bool(creds.get("access_token") or creds.get("refresh_token"))
        try:
            import base64
            encoded = str(creds.get("id_token") or "").split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            facts["subject"] = claims.get("sub") if isinstance(claims.get("sub"), str) else None
            if not facts["email"] and isinstance(claims.get("email"), str):
                facts["email"] = claims["email"]
        except (ValueError, IndexError, TypeError, AttributeError):
            pass
        if facts["present"] and not (facts["email"] or facts["subject"]) and creds.get("refresh_token"):
            facts["identity_ref"] = hashlib.sha256(str(creds["refresh_token"]).encode()).hexdigest()[:24]
        return facts

    @staticmethod
    def _truthy(value):
        return str(value or "").strip().lower() in ("1", "true", "yes", "on")

    def gemini_account(self):
        """The account Gemini CLI will use: {type: oauth|api_key|vertex, label, ...identity} or None when signed out.

        Precedence: an API key stored in the AgentFlow Vault, then Vertex (GOOGLE_GENAI_USE_VERTEXAI with a project or
        key), then the Google sign-in in ~/.gemini, then a GEMINI_API_KEY from the environment. Never contains secrets."""
        vault = self.providers.vault
        key = str(vault.get("GEMINI_API_KEY") or "").strip()
        stored = bool(str(vault.values.get("GEMINI_API_KEY") or "").strip())
        vertex = self._truthy(os.getenv("GOOGLE_GENAI_USE_VERTEXAI")) and bool(os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_CLOUD_PROJECT"))
        oauth = self.gemini_oauth()
        if stored or (key and not vertex and not oauth["present"]):
            return {"type": "api_key", "label": "Gemini API key", "key_fingerprint": hashlib.sha256(key.encode()).hexdigest()[:24]}
        if vertex:
            project = os.getenv("GOOGLE_CLOUD_PROJECT") or None
            return {"type": "vertex", "label": "Vertex AI" + (" · " + project if project else ""), "project": project,
                    "location": os.getenv("GOOGLE_CLOUD_LOCATION") or None,
                    "key_fingerprint": hashlib.sha256(os.environ["GOOGLE_API_KEY"].encode()).hexdigest()[:24] if os.getenv("GOOGLE_API_KEY") else None}
        if oauth["present"]:
            return {"type": "oauth", "email": oauth["email"], "label": oauth["email"] or "Google account (signed in)",
                    "subject": oauth["subject"], "identity_ref": oauth["identity_ref"]}
        if key:
            return {"type": "api_key", "label": "Gemini API key", "key_fingerprint": hashlib.sha256(key.encode()).hexdigest()[:24]}
        return None

    def has_identity(self, provider):
        if provider == "codex":
            return bool(self.scope_hint())
        if provider == "claude":
            return bool(self.claude_identity())
        if provider == "gemini":
            return bool(self.gemini_account())
        return False

    def scope_hint(self):
        # Read an optional stable account identifier, never copy or expose tokens.
        try:
            auth = json.loads((self.home("codex") / "auth.json").read_text(encoding="utf-8"))
            tokens=auth.get('tokens') or {}
            account_id=tokens.get('account_id') or auth.get('account_id')
            subject=None
            try:
                import base64
                encoded=str(tokens.get('id_token') or '').split('.')[1]
                claims=json.loads(base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4)))
                subject=claims.get('sub')
                account_id=account_id or (claims.get('https://api.openai.com/auth') or {}).get('chatgpt_account_id')
            except (ValueError,IndexError,TypeError,AttributeError):pass
            return hashlib.sha256(json.dumps([account_id,subject]).encode()).hexdigest() if account_id or subject else ''
        except (OSError, ValueError, TypeError, AttributeError):
            return ""

    def check(self, provider, force=False, account=_UNSET):
        """Confirm the signed-in identity. `account` lets a caller that already holds a fresh
        native account read (e.g. Codex app-server returning account + limits) skip a second spawn."""
        with self.lock:
            revision = self.revision(provider)
            if (not force and provider in self.current and self.revisions.get(provider) == revision
                    and provider in self.checked and time.monotonic() - self.checked[provider] < IDENTITY_TTL):
                return self.current[provider]
            if account is _UNSET:
                account = self.providers.read_account(provider)
            if self.revision(provider)!=revision:
                raise AccountChanged()
            if provider == "gemini":
                material = {"account": stable_identity(account) if account else None, "home": str(self.home(provider))}
            elif provider in ("codex", "claude"):
                material = {"account": stable_identity(account) if provider == "codex" else account, "home": str(self.home(provider)),
                            "auth": self.auth_config() if provider == "codex" else {},
                            "scope_hint": self.scope_hint() if provider == "codex" else ""}
            else:
                material = {"account": account, "revision": revision}
            fingerprint = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
            previous = self.current.get(provider) or self.persisted.get(provider) or {}
            label = str((account or {}).get("email") or (account or {}).get("label") or
                        ("Not signed in" if provider in ("codex", "claude", "gemini") and not account else "Current CLI account"))
            if (provider == "codex" and previous.get("fingerprint") and "plan_type" not in previous and previous.get("label") == label
                    and previous.get("type") == (account or {}).get("type")):
                # State written before fingerprints ignored plan/token fields: same account, keep its identity
                # so confirmed models and usage windows survive the upgrade.
                fingerprint = previous["fingerprint"]
            changed = bool(previous.get("fingerprint") and previous["fingerprint"] != fingerprint)
            state = {"fingerprint": fingerprint, "label": label,
                     "identity_digest":identity_digest(account),
                     "identity_key": self.claude_identity_key() if provider == "claude" else None,
                     "signed_in": bool(account) if provider in ("codex", "claude", "gemini") else None,
                     "type": (account or {}).get("type"), "checked_at": now(),"identity_revision":revision,
                     "plan_type": (account or {}).get("planType") or (account or {}).get("subscription_type"),
                     "changed_at": now() if changed else previous.get("changed_at")}
            if changed or not previous:
                self.providers.verified = {s for s in self.providers.verified if not s.startswith(provider + ":")}
            self.providers.models = [m for m in self.providers.models if m["provider"] != provider
                                     or m.get("account_fingerprint") == fingerprint]
            self.current[provider], self.persisted[provider] = state, state
            self.checked[provider], self.revisions[provider] = time.monotonic(), revision
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.persisted, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.path)
            if changed:
                for run_id, active in list(self.providers.active_accounts.items()):
                    if active["provider"] == provider and active["fingerprint"] != fingerprint:
                        self.providers.cancel(run_id)
                self.providers.store.event("Current account detected: " + provider + " current account",
                    "The previous account catalog and verification were cleared. Owner-selected models are retained.", "connection")
                if provider in ("codex", "claude", "gateway") and (provider == "gateway" or state["signed_in"]) and provider not in self.refreshing:
                    self.refreshing.add(provider)
                    try:
                        self.providers.refresh_models(provider)
                    except DomainError as exc:
                        self.providers.catalog_errors[provider] = str(exc)
                    finally:
                        self.refreshing.discard(provider)
            return state

    def assert_current(self, provider, fingerprint, force=True):
        if self.check(provider, force=force)["fingerprint"] != fingerprint:
            raise AccountChanged()
