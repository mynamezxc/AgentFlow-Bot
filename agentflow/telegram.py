"""Telegram bridge: long polling, safe group linking, routing, mirroring to the group, files and commands.

Everything that talks to Telegram goes through `http_call` / `http_download`, which redact the bot token from every
error. Outbound messages are queued in `channel_outbox` (channel "telegram_group") and delivered one at a time by the
sender thread, so order is kept and 429 / network errors are retried. The bot token is only ever read from the vault.
"""
import base64
import json
import mimetypes
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from . import __version__
from . import restarter
from .store import DomainError, now, uid
from .telegram_commands import TelegramCommands

API = "https://api.telegram.org"
POLL_TIMEOUT = 50
DOWNLOAD_LIMIT = 20 * 1024 * 1024
PHOTO_LIMIT = 10 * 1024 * 1024
DOCUMENT_LIMIT = 50 * 1024 * 1024
TEXT_LIMIT = 4096
CHUNK = 3800
ALLOWED_UPDATES = ["message", "edited_message", "callback_query", "my_chat_member"]
BOT_COMMANDS = [("help", "Show what I can do"), ("status", "Work status of every member"), ("showall", "List every setting"),
                ("get", "Show one setting: /get key"), ("set", "Change a setting: /set key value"), ("agents", "List the team"),
                ("goal", "Add a goal: /goal text"), ("files", "Latest deliverables"), ("approve", "Approval mode: /approve ask|auto"),
                ("stop", "Stop work: /stop [name|all]"), ("restart", "Restart AgentFlow"), ("unlink", "Unlink this chat")]
FORWARD_KEYS = ("questions", "permissions", "agent_messages", "status_digest", "files")
DEFAULT_CONFIG = {"default_agent_id": "a-lead",
                  "forward": {"questions": True, "permissions": True, "agent_messages": True, "status_digest": False, "files": True}}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
TOKEN_SHAPE = re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}")
FILE_COOLDOWN = 120
HELLO = {"vi": "Xin chào! Đây là tin nhắn test — Telegram đã kết nối với AgentFlow. Gõ /help để xem lệnh.",
         "en": "Hello! This is a test message — Telegram is connected to AgentFlow. Type /help to see the commands."}
ATTEMPTS = 6


class TelegramError(Exception):
    def __init__(self, code, description, retry_after=0):
        super().__init__(description)
        self.code, self.description, self.retry_after = code, description, retry_after


def redact(text, token=""):
    result = str(text)
    if token:
        result = result.replace(token, "[token]")
    return TOKEN_SHAPE.sub("bot[token]", result)


def multipart(fields, file):
    boundary = "----agentflow" + uuid.uuid4().hex
    body = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields)
    field, name, data, mime = file
    name = re.sub(r'[\r\n"\\]', "_", name)
    body += f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{name}"\r\nContent-Type: {mime}\r\n\r\n'.encode()
    return body + data + f"\r\n--{boundary}--\r\n".encode(), "multipart/form-data; boundary=" + boundary


def http_call(token, method, params=None, timeout=60, file=None):
    """One Telegram Bot API call. Returns `result`; raises TelegramError (token already removed from the text)."""
    url = f"{API}/bot{token}/{method}"
    params = params or {}
    if file:
        fields = [(k, v if isinstance(v, str) else json.dumps(v)) for k, v in params.items() if v is not None]
        body, content_type = multipart(fields, file)
    else:
        body, content_type = json.dumps(params).encode(), "application/json"
    request = urllib.request.Request(url, data=body, headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            info = json.loads(exc.read() or b"{}")
        except ValueError:
            info = {}
        raise TelegramError(exc.code, redact(info.get("description") or "HTTP %s" % exc.code, token),
                            int((info.get("parameters") or {}).get("retry_after") or 0)) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise TelegramError(0, redact("Cannot reach Telegram (%s)" % type(exc).__name__, token)) from None
    if not payload.get("ok"):
        raise TelegramError(int(payload.get("error_code") or 0), redact(payload.get("description") or "Telegram error", token),
                            int((payload.get("parameters") or {}).get("retry_after") or 0))
    return payload.get("result")


def http_download(token, file_path, limit=DOWNLOAD_LIMIT):
    request = urllib.request.Request(f"{API}/file/bot{token}/{file_path}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(limit + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TelegramError(0, redact("Cannot download the file (%s)" % type(exc).__name__, token)) from None
    if len(data) > limit:
        raise TelegramError(413, "The file is larger than %d MB." % (limit // 1024 // 1024))
    return data


# ---- text helpers --------------------------------------------------------------------------------------------

def esc(text, quote=False):
    result = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return result.replace('"', "&quot;") if quote else result


def to_html(markdown):
    """Small Markdown subset (code, bold, links, headings) to Telegram HTML; everything else is escaped text."""
    out = []
    for part in re.split(r"(```[\s\S]*?```)", str(markdown)):
        if len(part) >= 6 and part.startswith("```") and part.endswith("```"):
            code = re.sub(r"^[A-Za-z0-9_+-]*\n", "", part[3:-3], count=1)
            out.append("<pre>" + esc(code.strip("\n")) + "</pre>")
            continue
        text = esc(part)
        text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
        text = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", text)
        text = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"<b>\1</b>", text)
        text = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)<>\"]+)\)", lambda m: '<a href="%s">%s</a>' % (m.group(2).replace('"', "&quot;"), m.group(1)), text)
        out.append(text)
    return "".join(out)


def html_chunks(text, markdown=True, limit=CHUNK):
    """Pieces that each render to at most `limit` characters of HTML (4096 is Telegram's hard limit)."""
    render = to_html if markdown else esc
    lines, pieces, current = str(text).split("\n"), [], ""
    def fits(candidate):
        return len(render(candidate)) <= limit
    for line in lines:
        candidate = current + ("\n" if current else "") + line
        if fits(candidate):
            current = candidate
            continue
        if current:
            pieces.append(current)
            current = ""
        while not fits(line):
            cut = max(1, len(line) * 3 // 4)
            while cut > 1 and not fits(line[:cut]):
                cut = cut * 3 // 4
            pieces.append(line[:cut])
            line = line[cut:]
        current = line
    if current or not pieces:
        pieces.append(current)
    return [render(p) for p in pieces]


def plain(html_text):
    return re.sub(r"<[^>]+>", "", str(html_text)).replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&")


def file_plan(size, suffix):
    """How a file leaves: "photo" (images up to 10 MB), "document" (up to 50 MB) or "note" (too big to upload)."""
    if size > DOCUMENT_LIMIT:
        return "note"
    return "photo" if suffix.lower() in IMAGE_SUFFIXES and size <= PHOTO_LIMIT else "document"


def rel_time(stamp, at=None):
    """English relative time, same wording as the web app: just now, 30s ago, 5 min ago, 2 h ago, yesterday, a date."""
    from datetime import datetime, timezone
    try:
        then = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return ""
    current = at or datetime.now(timezone.utc)
    seconds = (current - then).total_seconds()
    if seconds < 10:
        return "just now"
    if seconds < 60:
        return "%ds ago" % seconds
    if seconds < 3600:
        return "%d min ago" % (seconds // 60)
    if seconds < 86400:
        return "%d h ago" % (seconds // 3600)
    if (current.astimezone().date() - then.astimezone().date()).days <= 1:
        return "yesterday"
    return then.astimezone().strftime("%b %d") if then.year == current.year else then.astimezone().strftime("%b %d, %Y")


def display_name(user):
    user = user or {}
    return ("@" + user["username"]) if user.get("username") else " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or "owner"


class TelegramBridge(TelegramCommands):
    def __init__(self, engine):
        self.engine, self.store, self.vault = engine, engine.store, engine.vault
        self.http, self.download = http_call, http_download
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.RLock()
        self.threads = []
        self.jobs = []                   # (due, kind, id) mirror work, filled by store listeners
        self.pending = []                # outbox ids waiting for delivery, oldest first
        self.cooldown_until = 0.0
        self.answered_by = {}            # decision id -> who answered from Telegram
        self.file_sent_at = {}
        self.halted = ""                 # "401" | "409" once polling must not continue
        self.generation = 0              # bumped by close(): threads of an older generation stop at their next step
        self.reload_lock = threading.Lock()
        self.attached = False
        self.state_path = self.store.directory / "telegram-state.json"
        self.cache = None
        engine.decision_handlers["telegram_link"] = self.link_decision

    # ---- settings and state ----------------------------------------------------------------------------------
    def token(self):
        return self.vault.get("TELEGRAM_BOT_TOKEN") or ""

    def mode(self):
        value = self.store.settings().get("telegram_mode")
        return value if value in ("polling", "webhook") else "polling"

    def raw_config(self):
        value = self.store.settings().get("telegram")
        return dict(value) if isinstance(value, dict) else {}

    def config(self):
        raw = self.raw_config()
        return {**DEFAULT_CONFIG, **raw, "forward": {**DEFAULT_CONFIG["forward"], **(raw.get("forward") if isinstance(raw.get("forward"), dict) else {})}}

    def linked(self):
        return bool(self.raw_config().get("chat_id"))

    def chat_id(self):
        return str(self.raw_config().get("chat_id") or "")

    def owners(self):
        return {int(x) for x in self.raw_config().get("owner_user_ids", []) if str(x).lstrip("-").isdigit()}

    def default_agent(self):
        for candidate in (self.raw_config().get("default_agent_id"), self.store.settings().get("telegram_agent_id"), "a-lead"):
            try:
                agent = self.store.get("agents", candidate) if candidate else None
            except DomainError:
                agent = None
            if agent and not agent.get("archived"):
                return agent["id"]
        return "a-lead"

    def validate_config(self, value, current=None):
        """settings.telegram from the web UI: toggles, default agent and owner users only. The chat itself is linked by an
        owner decision and unlinked by /unlink or the Unlink button, never by a free-form settings write."""
        if not isinstance(value, dict) or set(value) - {"default_agent_id", "forward", "owner_user_ids"}:
            raise DomainError("Telegram settings accept default_agent_id, forward and owner_user_ids.")
        result = dict(current if current is not None else self.raw_config())
        if "default_agent_id" in value:
            agent = self.store.get("agents", value["default_agent_id"]) if isinstance(value["default_agent_id"], str) else None
            if not agent or agent.get("archived"):
                raise DomainError("Choose an active agent to receive Telegram messages.")
            result["default_agent_id"] = agent["id"]
        if "forward" in value:
            forward = value["forward"]
            if not isinstance(forward, dict) or set(forward) - set(FORWARD_KEYS) or any(not isinstance(v, bool) for v in forward.values()):
                raise DomainError("Telegram forwarding switches are questions, permissions, agent_messages, status_digest and files (on or off).")
            result["forward"] = {**DEFAULT_CONFIG["forward"], **(result.get("forward") or {}), **forward}
        if "owner_user_ids" in value:
            ids = value["owner_user_ids"]
            if not result.get("chat_id"):
                raise DomainError("Link a Telegram chat before choosing owner users.")
            if not isinstance(ids, list) or len(ids) > 10 or any(isinstance(i, bool) or not isinstance(i, int) or i <= 0 for i in ids):
                raise DomainError("Owner users are numeric Telegram user IDs (at most 10).")
            if not ids:
                raise DomainError("Keep at least one owner user, or unlink the chat.")
            result["owner_user_ids"] = list(dict.fromkeys(ids))
        return result

    def state(self):
        with self.lock:
            if self.cache is None:
                try:
                    self.cache = json.loads(self.state_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    self.cache = {}
            return self.cache

    def save(self):
        with self.lock:
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.cache or {}), encoding="utf-8")
            temporary.replace(self.state_path)

    def remember(self, **changes):
        with self.lock:
            self.state().update(changes)
            self.save()

    def mutate(self, change):
        with self.lock:
            change(self.state())
            self.save()

    def trim(self, name, keep):
        data = self.state().get(name)
        if isinstance(data, dict) and len(data) > keep:
            for key in list(data)[:len(data) - keep]:
                data.pop(key, None)

    def button(self, text, payload):
        """Inline button whose callback_data is a short server-side id (Telegram allows 64 bytes)."""
        token = secrets.token_urlsafe(6)
        def add(state):
            state.setdefault("cb", {})[token] = payload
            self.trim("cb", 400)
        self.mutate(add)
        return {"text": str(text)[:60] or "-", "callback_data": "cb:" + token}

    def payload_of(self, data):
        return (self.state().get("cb") or {}).get(str(data)[3:]) if str(data).startswith("cb:") else None

    def public_status(self):
        state, config = self.state(), self.config()
        return {"configured": bool(self.token()), "mode": self.mode(), "connected": bool(state.get("connected")) and not self.halted,
                "username": state.get("username") or "", "last_update_at": state.get("last_update_at"), "last_error": state.get("last_error") or "",
                "halted": self.halted, "linked": self.linked(), "chat_title": config.get("chat_title") or "", "chat_type": config.get("chat_type") or "",
                "owner_user_ids": list(config.get("owner_user_ids") or []), "default_agent_id": self.default_agent(), "forward": config["forward"],
                "pending": [{"chat_id": k, "title": v.get("title"), "user": v.get("user")} for k, v in (state.get("pending") or {}).items()],
                "polling": any(t.name == "agentflow-telegram-poll" and t.is_alive() for t in self.threads), "linked_at": config.get("linked_at")}

    # ---- lifecycle -------------------------------------------------------------------------------------------
    def start(self, ready=False):
        if not self.token():
            self.announce_restart()   # the web chat still gets the "restarted" line without a bot
            return False
        if self.threads:
            return False
        self.attach()
        self.restore_outbox()
        self.stop.clear()
        generation = self.generation
        specs = [("agentflow-telegram-out", self.sender_loop, (generation,))]
        if self.mode() == "polling":
            specs.append(("agentflow-telegram-poll", self.poll_loop, (generation, ready)))
        for name, target, args in specs:
            thread = threading.Thread(target=target, args=args, daemon=True, name=name)
            thread.start()
            self.threads.append(thread)
        if self.mode() != "polling" and not ready:
            self.announce_restart()
        return True

    @staticmethod
    def valid_token(value):
        value = str(value or "").strip()
        if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", value):
            raise DomainError("That does not look like a Telegram bot token. Copy it from @BotFather (it looks like 123456789:ABC...).")
        return value

    def reload(self):
        """Apply a new, replaced or removed token or a changed mode without restarting the runtime: stop the current poller and
        sender, then connect again (getMe, deleteWebhook in polling mode, setMyCommands) and start new threads. Returns the status."""
        with self.reload_lock:
            self.close()
            self.halted = ""
            self.cooldown_until = 0.0
            self.cache = None
            self.remember(connected=False, last_error="", **({} if self.token() else {"username": ""}))
            if not self.token():
                return self.public_status()
            ready = False
            try:
                self.setup()
                ready = True
            except TelegramError as exc:
                if self.on_error(exc) == "stop":
                    return self.public_status()
            except Exception:
                self.event("Telegram did not start", "Telegram could not be set up. It will retry.", "error")
            self.start(ready=ready)
            return self.public_status()

    def clear_token(self):
        """Remove the saved bot token and stop Telegram (a token from .env or the environment still counts)."""
        self.vault.set({}, replace_prefix="TELEGRAM_BOT_TOKEN")
        status = self.reload()
        return {"cleared": True, "from_environment": bool(self.token()), "telegram": status}

    def close(self, wait=3):
        self.generation += 1
        self.stop.set()
        self.wake.set()
        for thread in self.threads:
            thread.join(timeout=wait)
        self.threads = []
        if self.attached:
            try:
                self.store.listeners.remove(self.on_store)
            except ValueError:
                pass
            self.attached = False

    def attach(self):
        if not self.attached:
            self.store.listeners.append(self.on_store)
            self.attached = True

    def event(self, title, detail="", kind="system"):
        self.store.event(title, redact(detail, self.token()), kind)

    def call(self, method, params=None, timeout=60, file=None):
        return self.http(self.token(), method, params or {}, timeout, file)

    # ---- polling ---------------------------------------------------------------------------------------------
    def setup(self):
        """getMe, drop an old webhook (polling mode) and publish the command menu. Raises TelegramError."""
        me = self.call("getMe", timeout=20)
        self.remember(username=me.get("username") or "", bot_id=me.get("id"), connected=True, last_error="")
        if self.mode() == "polling":
            self.call("deleteWebhook", {"drop_pending_updates": False}, timeout=20)
        try:
            self.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in BOT_COMMANDS]}, timeout=20)
        except TelegramError as exc:
            self.event("Telegram command menu not updated", exc.description, "error")
        self.announce_restart()
        return me

    def on_error(self, exc):
        """"stop" when polling must not continue; otherwise the seconds to wait before the next try."""
        self.remember(last_error=exc.description, last_error_at=now(), connected=False if exc.code in (401, 409) else self.state().get("connected", False))
        if exc.code == 401:
            self.halted = "401"
            self.event("Telegram token rejected", "Telegram refused the bot token. Update it in Connections and restart.", "error")
            return "stop"
        if exc.code == 409:
            self.halted = "409"
            self.event("Telegram polling stopped", "Another program or a webhook is using this bot token. Stop it, then restart AgentFlow.", "error")
            return "stop"
        if exc.code == 429:
            return min(max(1, exc.retry_after), 120)
        return None

    def poll_loop(self, generation=None, ready=False):
        delay = 2
        generation = self.generation if generation is None else generation
        while not self.stop.is_set() and generation == self.generation:
            try:
                if not ready:
                    self.setup()
                    ready = True
                self.poll_once(timeout=POLL_TIMEOUT, generation=generation)
                delay = 2
            except TelegramError as exc:
                if generation != self.generation:
                    return   # a replaced poller: its errors (such as 409 from the new one) are not news
                verdict = self.on_error(exc)
                if verdict == "stop":
                    return
                wait = verdict if isinstance(verdict, int) else delay
                if verdict is None:
                    delay = min(60, delay * 2)
                if self.stop.wait(wait):
                    return
            except Exception:
                self.event("Telegram update failed", "An update could not be processed. Polling continues.", "error")
                if self.stop.wait(delay):
                    return

    def poll_once(self, timeout=0, generation=None):
        offset = int(self.state().get("offset") or 0)
        updates = self.call("getUpdates", {"offset": offset, "timeout": timeout, "allowed_updates": ALLOWED_UPDATES}, timeout=timeout + 15) or []
        if generation is not None and generation != self.generation:
            return 0   # the token or mode changed while this call was waiting: the new poller owns the updates
        for update in updates:
            update_id = int(update.get("update_id", 0))
            try:
                self.handle_update(update)
            except Exception as exc:
                self.event("Telegram update failed", type(exc).__name__ + " while handling update " + str(update_id), "error")
            self.remember(offset=max(offset, update_id + 1), last_update_at=now())
            offset = max(offset, update_id + 1)
        return len(updates)

    # ---- inbound -------------------------------------------------------------------------------------------------
    def handle_update(self, update):
        update_id = update.get("update_id")
        if update_id is not None and not self.store.reserve_delivery("telegram:poll:%s" % update_id):
            return "duplicate"
        if update.get("callback_query"):
            return self.on_callback(update["callback_query"])
        if update.get("my_chat_member"):
            return self.on_membership(update["my_chat_member"])
        message = update.get("message")
        if not message:
            return "ignored"
        chat, sender = message.get("chat") or {}, message.get("from") or {}
        if not chat.get("id"):
            return "ignored"
        if str(chat["id"]) != self.chat_id():
            return self.unknown_chat(chat, sender)
        if sender.get("id") not in self.owners() or sender.get("is_bot"):
            return self.stranger(chat, sender)
        return self.owner_message(message, sender)

    def on_membership(self, change):
        chat = change.get("chat") or {}
        status = (change.get("new_chat_member") or {}).get("status")
        if str(chat.get("id")) == self.chat_id() and status in ("left", "kicked"):
            self.event("Telegram bot removed", "The bot was removed from '" + str(chat.get("title") or "the linked chat") + "'.", "error")
        return "membership"

    def stranger(self, chat, sender):
        """A member who is not an owner: ignored; one short notice per person so nobody is left guessing."""
        key = str(sender.get("id"))
        if sender.get("is_bot") or key in (self.state().get("notified") or []):
            return "ignored"
        self.mutate(lambda s: s.setdefault("notified", []).append(key))
        self.say(chat["id"], "Only the owner linked in AgentFlow can control the team from this chat.")
        return "stranger"

    def anchor_run(self):
        try:
            return self.store.get("runs", "telegram-anchor")
        except DomainError:
            return self.store.create("runs", {"id": "telegram-anchor", "agent_id": "a-lead", "prompt": "Telegram connection", "kind": "telegram",
                "status": "done", "steps": 0, "trace": [], "phase": "done", "pending_calls": [], "wait_for_runs": [], "wait_for_tasks": [],
                "wait_for_decisions": [], "root_id": "telegram-anchor", "channel": "dm-a-lead", "contract_version": 2, "retry_count": 0,
                "system_managed": True, "no_followup": True, "completed_at": now()})

    def unknown_chat(self, chat, sender):
        chat_id = str(chat["id"])
        state = self.state()
        if chat_id in (state.get("blocked") or []):
            return "blocked"
        if chat_id in (state.get("pending") or {}):
            return "pending"
        if sender.get("is_bot"):
            return "ignored"
        kind = chat.get("type") or "private"
        title = chat.get("title") or display_name(sender) or kind
        who = display_name(sender)
        replaces = (" This replaces the current link to '" + str(self.config().get("chat_title")) + "'.") if self.linked() else ""
        question = "Link Telegram %s '%s' (id %s) to AgentFlow? Messages from %s there will act as you.%s" % (kind, title[:80], chat_id, who, replaces)
        asked = self.engine.team.ask(self.store.get("agents", "a-lead"), {"question": question, "blocking": False, "options": ["Link", "Ignore"],
            "context": "Only link a chat you control. Anyone in a linked group who is not listed as an owner is ignored. You can unlink it any time.",
            "actions": [{"value": "link", "label": "Link", "style": "primary"}, {"value": "ignore", "label": "Ignore", "style": "quiet"}],
            "action_field": "telegram_link_action", "action_handler": "telegram_link"}, self.anchor_run(), _routed=True)
        self.store.patch("decisions", asked["decision_id"], {"telegram_link": {"chat_id": chat_id, "title": title[:120], "type": kind,
            "user_id": sender.get("id"), "user": who}})
        self.mutate(lambda s: s.setdefault("pending", {}).__setitem__(chat_id, {"decision_id": asked["decision_id"], "title": title[:120], "user": who}))
        self.say(chat_id, "Waiting for approval in AgentFlow.")
        return "asked"

    def link_decision(self, question, answer):
        """decision_handlers["telegram_link"]: runs inside Collaboration.answer."""
        info = question.get("telegram_link") or {}
        chat_id = str(info.get("chat_id") or "")
        if not chat_id:
            return
        def done(state):
            (state.get("pending") or {}).pop(chat_id, None)
        if answer.get("telegram_link_action") != "link":
            def block(state):
                done(state)
                state.setdefault("blocked", []).append(chat_id)
            self.mutate(block)
            self.event("Telegram chat ignored", "'%s' will not be able to reach AgentFlow." % info.get("title", chat_id))
            return
        previous = self.config()
        owners = [info["user_id"]] if isinstance(info.get("user_id"), int) else []
        cfg = {"chat_id": chat_id, "chat_title": info.get("title") or "", "chat_type": info.get("type") or "", "owner_user_ids": owners,
               "linked_at": now(), "default_agent_id": previous.get("default_agent_id") or "a-lead", "forward": {**DEFAULT_CONFIG["forward"]}}
        chats = [c.strip() for c in str(self.store.settings().get("telegram_chats", "")).split(",") if c.strip()]
        if previous.get("chat_id") in chats:
            chats.remove(previous["chat_id"])
        if chat_id not in chats:
            chats.append(chat_id)
        self.store.settings({"telegram": cfg, "telegram_chats": ",".join(chats)})
        self.mutate(lambda s: (done(s), s.update(notified=[])))
        self.event("Telegram chat linked", "'%s' (%s) now controls AgentFlow for the listed owner." % (cfg["chat_title"], chat_id))
        self.note_linked(cfg["chat_title"] or chat_id)
        self.say(chat_id, html=self.hello_html())

    def owner_lang(self):
        """Language the owner wrote last in the default agent's web chat, else in any chat, else English."""
        code = self.engine.channel_language("dm-" + self.default_agent())
        if not code:
            code = next((r["owner_language"] for r in reversed(self.store.all("runs")) if r.get("kind") == "chat" and r.get("owner_language")), None)
        return code or "en"

    def hello_html(self):
        """The fixed greeting posted when a chat is linked and by "Send test message" (no model call)."""
        agent = self.store.get("agents", self.default_agent())
        name = agent["name"] + (" - " + agent["role"] if agent.get("role") else "")
        return "<b>%s</b>: %s" % (esc(name), esc(HELLO.get(self.owner_lang(), HELLO["en"])))

    def note_linked(self, title):
        """One line in the web chat of the default agent; it is not mirrored back to Telegram."""
        agent_id = self.default_agent()
        try:
            self.engine.team.emit(agent_id, "Telegram linked: " + str(title), {**self.anchor_run(), "channel": "dm-" + agent_id}, "chat", telegram_skip=True)
        except DomainError:
            pass

    def unlink(self):
        cfg = self.raw_config()
        chat_id = str(cfg.get("chat_id") or "")
        chats = [c.strip() for c in str(self.store.settings().get("telegram_chats", "")).split(",") if c.strip() and c.strip() != chat_id]
        self.store.settings({"telegram": {}, "telegram_chats": ",".join(chats)})
        self.mutate(lambda s: s.update(notified=[], posted={}, msgs={}))
        self.event("Telegram chat unlinked", "'%s' no longer controls AgentFlow." % cfg.get("chat_title", chat_id))
        return chat_id

    # ---- owner messages ------------------------------------------------------------------------------------------------
    def split_agent(self, text):
        """(agent or None, rest). `@Name text` or `Name: text` at the start; raises DomainError when the name is ambiguous."""
        clean = text.lstrip()
        bot = self.state().get("username")
        if bot:
            clean = re.sub(r"^@" + re.escape(bot) + r"\b[:,]?\s*", "", clean, flags=re.I)
        best, hits = 0, []
        for agent in self.store.all("agents"):
            if agent.get("archived"):
                continue
            words = agent["name"].split()
            for name in {agent["name"].casefold(), (words[0] if words else "").casefold()} - {""}:
                for pattern in (r"^@" + re.escape(name) + r"(?=[\s:,]|$)[:,]?\s*", r"^" + re.escape(name) + r"\s*:\s*"):
                    match = re.match(pattern, clean, re.I)
                    if match:
                        if len(name) > best:
                            best, hits = len(name), []
                        if len(name) == best and agent["id"] not in [h[0]["id"] for h in hits]:
                            hits.append((agent, clean[match.end():].strip()))
        if not hits:
            return None, text
        if len(hits) > 1:
            raise DomainError("Several teammates match that name (%s). Use the full name." % ", ".join(h[0]["name"] for h in hits))
        if not hits[0][1]:
            raise DomainError("Add a message after the name.")
        return hits[0]

    def collect_files(self, message):
        """Photos and documents of a message as attachment ids (Telegram getFile, up to 20 MB)."""
        ids = []
        photos = message.get("photo")
        document = message.get("document")
        target = None
        if photos:
            best = max(photos, key=lambda p: p.get("file_size") or p.get("width", 0) * p.get("height", 0))
            target = {"file_id": best["file_id"], "name": "photo-%s.jpg" % str(best.get("file_unique_id") or uid("p"))[:12], "size": best.get("file_size")}
        elif document:
            target = {"file_id": document["file_id"], "name": document.get("file_name") or "file", "size": document.get("file_size")}
        if not target:
            return ids
        if (target["size"] or 0) > DOWNLOAD_LIMIT:
            raise DomainError("Telegram only lets bots download files up to 20 MB.")
        info = self.call("getFile", {"file_id": target["file_id"]}, timeout=30)
        data = self.download(self.token(), info["file_path"], DOWNLOAD_LIMIT)
        upload = self.engine.attachments.upload({"name": target["name"], "data": base64.b64encode(data).decode()})
        ids.append(upload["id"])
        return ids

    def owner_message(self, message, sender):
        chat_id = str(message["chat"]["id"])
        text = (message.get("text") or message.get("caption") or "").strip()
        who = display_name(sender)
        try:
            command = self.parse_command(text)
            if command:
                return self.command(command[0], command[1], message, sender)
            reply = message.get("reply_to_message") or {}
            target = (self.state().get("msgs") or {}).get(str(reply.get("message_id"))) if reply.get("message_id") else None
            if target and target.get("t") == "decision":
                self.answer_decision(target["id"], text, who)
                return "answered"
            agent_id = target["id"] if target and target.get("t") == "agent" else None
            if not agent_id:
                agent, rest = self.split_agent(text)
                agent_id, text = (agent["id"], rest) if agent else (self.default_agent(), text)
            attachments = self.collect_files(message)
            if not text and not attachments:
                return "ignored"
            self.engine.chat({"agent_id": agent_id, "channel": "dm-" + agent_id, "message": text, "attachment_ids": attachments})
            return "routed:" + agent_id
        except DomainError as exc:
            self.say(chat_id, str(exc))
            return "error"
        except TelegramError as exc:
            self.say(chat_id, "Telegram could not give me that file: " + exc.description)
            return "error"

    def answer_decision(self, decision_id, text, who):
        """Answer an open decision from typed text or a button. Option answers must match exactly; free text needs no match."""
        decision = self.store.get("decisions", decision_id)
        if decision["status"] != "open":
            raise DomainError("That question was already answered.")
        choices = [(a["label"], a["value"]) for a in (decision.get("actions") or [])] or [(o["label"], o["value"]) for o in decision.get("options") or []]
        answer = str(text).strip()
        if not answer:
            raise DomainError("Send your answer as text.")
        if choices:
            folded = answer.casefold()
            match = next((label for index, (label, value) in enumerate(choices, 1) if folded in (label.casefold(), str(value).casefold(), str(index))), None)
            if match is None and decision.get("kind") != "input":
                raise DomainError("Choose one of: " + "; ".join("%d. %s" % (i, l) for i, (l, v) in enumerate(choices, 1)))
            answer = match or answer
        self.answered_by[decision_id] = who
        try:
            return self.engine.team.answer(decision_id, {"answer": answer})
        except Exception:
            self.answered_by.pop(decision_id, None)
            raise

    def on_callback(self, query):
        data = query.get("data") or ""
        user = query.get("from") or {}
        chat = (query.get("message") or {}).get("chat") or {}
        def reply(text, alert=False):
            try:
                self.call("answerCallbackQuery", {"callback_query_id": query.get("id"), "text": str(text)[:190], "show_alert": alert}, timeout=15)
            except TelegramError:
                pass
        if str(chat.get("id")) != self.chat_id() or user.get("id") not in self.owners():
            reply("Not allowed.", True)
            return "denied"
        payload = self.payload_of(data)
        if not payload:
            reply("This button has expired.", True)
            return "expired"
        try:
            kind = payload.get("t")
            if kind == "dec":
                self.answer_decision(payload["id"], payload["label"], display_name(user))
                reply("Answered")
            elif kind == "cfg":
                self.callback_config(payload, query, reply)
            elif kind == "confirm":
                self.callback_confirm(payload, query, reply)
            elif kind == "file":
                reply("Sending")
                self.send_artifact(payload["id"], force=True)
            else:
                reply("This button has expired.", True)
        except DomainError as exc:
            reply(str(exc), True)
        return "callback"

    # ---- outbound queue -------------------------------------------------------------------------------------------------
    def enqueue(self, method, params, key=None, ref=None, file=None):
        chat_id = str(params.get("chat_id"))
        item = self.store.create("channel_outbox", {"outbound": {"channel": "telegram_group", "recipient": chat_id, "event_key": key or uid("tg")},
            "content": plain(params.get("text") or params.get("caption") or method)[:200], "status": "queued", "attempts": 0,
            "tg": {"method": method, "params": params, **({"file": file} if file else {})}, **({"ref": ref} if ref else {})})
        with self.lock:
            self.pending.append(item["id"])
        self.wake.set()
        return item["id"]

    def say(self, chat_id, text="", html=None, keyboard=None, ref=None, key=None, markdown=False):
        """Queue a message (HTML, split at 4096). `text` is shown literally unless markdown=True; `html` is already safe."""
        if html is not None:
            pieces = html_chunks(plain(html), markdown=False) if len(html) > TEXT_LIMIT - 96 else [html]
        else:
            pieces = html_chunks(self.vault.redact(text), markdown=markdown)
        for index, piece in enumerate(pieces):
            params = {"chat_id": chat_id, "text": piece, "parse_mode": "HTML", "disable_web_page_preview": True}
            if keyboard and index == len(pieces) - 1:
                params["reply_markup"] = {"inline_keyboard": keyboard}
            self.enqueue("sendMessage", params, key=(key + ":%d" % index) if key else None, ref=ref)

    def restore_outbox(self):
        waiting = [i["id"] for i in self.store.all("channel_outbox") if i["outbound"].get("channel") == "telegram_group" and i["status"] == "queued"]
        with self.lock:
            self.pending.extend(i for i in waiting if i not in self.pending)

    def sender_loop(self, generation=None):
        generation = self.generation if generation is None else generation
        while not self.stop.is_set() and generation == self.generation:
            self.wake.wait(1.0)
            self.wake.clear()
            if generation != self.generation:
                return
            if self.halted == "401":
                self.stop.wait(5)
                continue
            try:
                self.run_jobs()
                self.flush()
            except Exception:
                self.event("Telegram delivery failed", "A queued Telegram message could not be processed; it will be retried.", "error")
                self.stop.wait(2)

    def flush(self, limit=200):
        """Deliver due outbox items in order. Items waiting for a retry are skipped; returns how many were sent."""
        sent = 0
        while sent < limit and time.time() >= self.cooldown_until and not self.stop.is_set():
            with self.lock:
                ids = list(self.pending)
            progress = False
            for item_id in ids:
                try:
                    item = self.store.get("channel_outbox", item_id)
                except DomainError:
                    self.drop(item_id)
                    continue
                if item["status"] != "queued":
                    self.drop(item_id)
                    continue
                if item.get("retry_at", 0) > time.time():
                    continue
                if self.deliver(item):
                    self.drop(item_id)
                    sent += 1
                    progress = True
                    break
                if time.time() < self.cooldown_until:
                    break
            if not progress:
                break
        return sent

    def drop(self, item_id):
        with self.lock:
            if item_id in self.pending:
                self.pending.remove(item_id)

    def deliver(self, item):
        """True when the item is finished (sent or given up); False when it stays queued for a retry."""
        tg = item["tg"]
        params = dict(tg["params"])
        file = None
        if tg.get("file"):
            try:
                path = Path(tg["file"]["path"])
                file = (tg["file"]["field"], tg["file"]["name"], path.read_bytes(), tg["file"].get("mime") or "application/octet-stream")
            except OSError:
                self.store.patch("channel_outbox", item["id"], {"status": "failed", "error": "The file is no longer available."})
                return True
        try:
            result = self.call(tg["method"], params, timeout=120 if file else 30, file=file)
        except TelegramError as exc:
            return self.failed(item, exc)
        self.store.patch("channel_outbox", item["id"], {"status": "sent", "completed_at": now()})
        self.delivered(item, result)
        self.remember(last_sent_at=now())
        return True

    def failed(self, item, exc):
        tg, attempts = item["tg"], int(item.get("attempts", 0)) + 1
        text = exc.description.casefold()
        if exc.code == 429:
            self.cooldown_until = time.time() + min(max(1, exc.retry_after), 300)
            return False
        if exc.code == 401:
            self.halted = "401"
            self.on_error(exc)
            return False
        if exc.code == 400 and "can't parse entities" in text and tg["params"].get("parse_mode"):
            params = {k: v for k, v in tg["params"].items() if k != "parse_mode"}
            for key in ("text", "caption"):
                if key in params:
                    params[key] = plain(params[key])
            self.store.patch("channel_outbox", item["id"], {"tg": {**tg, "params": params}, "attempts": attempts})
            return False
        if exc.code == 400 and tg["method"] == "sendPhoto":
            params = dict(tg["params"])
            file = dict(tg["file"], field="document")
            self.store.patch("channel_outbox", item["id"], {"tg": {"method": "sendDocument", "params": params, "file": file}, "attempts": attempts})
            return False
        if exc.code == 400 and "not modified" in text:
            self.store.patch("channel_outbox", item["id"], {"status": "sent", "completed_at": now()})
            return True
        if exc.code in (400, 403, 404) or attempts >= ATTEMPTS:
            self.store.patch("channel_outbox", item["id"], {"status": "failed", "error": exc.description, "attempts": attempts})
            self.remember(last_error=exc.description, last_error_at=now())
            self.event("Telegram message not delivered", exc.description, "error")
            return True
        self.store.patch("channel_outbox", item["id"], {"attempts": attempts, "retry_at": time.time() + min(60, 2 ** attempts)})
        return False

    def delivered(self, item, result):
        ref = item.get("ref")
        message_id = (result or {}).get("message_id") if isinstance(result, dict) else None
        if not ref or not message_id:
            return
        def note(state):
            state.setdefault("msgs", {})[str(message_id)] = ref
            self.trim("msgs", 600)
            if ref.get("t") == "decision":
                posted = (state.get("posted") or {}).get(ref["id"])
                if posted is not None:
                    posted["message_id"] = message_id
        self.mutate(note)
        if ref.get("t") == "decision":
            try:
                if self.store.get("decisions", ref["id"])["status"] != "open":
                    self.edit_answered(ref["id"])
            except DomainError:
                pass

    # ---- mirroring: store listener -> jobs -> messages ----------------------------------------------------------------------------
    def on_store(self, event, kind, doc):
        """Called by the store for every write: cheap filtering only, real work happens on the sender thread."""
        if not self.linked():
            return
        due = time.time()
        if kind == "messages" and event == "create" and doc.get("role") != "user":
            job = (due + 0.3, "message", doc["id"])
        elif kind == "decisions" and event == "create":
            job = (due + 1.5, "decision", doc["id"])
        elif kind == "decisions" and event == "patch" and doc.get("status") == "answered":
            job = (due, "answered", doc["id"])
        elif kind == "artifacts" and doc.get("deliverable") and not doc.get("external"):
            job = (due + 1.0, "artifact", doc["id"])
        else:
            return
        with self.lock:
            self.jobs.append(job)
        self.wake.set()

    def run_jobs(self, force=False):
        """Process mirror jobs that are due (all of them with force=True, used by tests)."""
        due, later = [], []
        with self.lock:
            for job in self.jobs:
                (due if force or job[0] <= time.time() else later).append(job)
            self.jobs = later
        for _, kind, record_id in due:
            try:
                getattr(self, "mirror_" + kind)(record_id)
            except DomainError:
                continue
        return len(due)

    @staticmethod
    def owner_visible(message):
        if message.get("audience"):
            return message["audience"] == "owner"
        return message.get("kind") not in ("peer", "handoff", "peer_reply")

    def who(self, agent_id):
        try:
            agent = self.store.get("agents", agent_id)
            return "<b>%s</b>%s" % (esc(agent["name"]), (" · " + esc(agent["role"])) if agent.get("role") else "")
        except DomainError:
            return "<b>Agent</b>"

    def mirror_message(self, message_id):
        message = self.store.get("messages", message_id)
        cfg = self.config()
        chat_id = self.chat_id()
        if not chat_id or message.get("role") == "user" or not self.owner_visible(message):
            return
        if message.get("telegram_skip") or message.get("kind") in ("decision", "decision_answer", "action") or str(message.get("channel", "")).startswith("telegram:"):
            return
        if not self.store.reserve_delivery("tg:msg:" + message_id):
            return
        text = self.vault.redact(str(message.get("content") or "")).strip()
        if text and cfg["forward"]["agent_messages"]:
            header = self.who(message.get("agent_id"))
            pieces = html_chunks(text, markdown=True)
            for index, piece in enumerate(pieces):
                self.enqueue("sendMessage", {"chat_id": chat_id, "text": (header + "\n" if index == 0 else "") + piece, "parse_mode": "HTML",
                    "disable_web_page_preview": True}, key="tg:msg:%s:%d" % (message_id, index), ref={"t": "agent", "id": message.get("agent_id")})
        for artifact_id in (message.get("artifact_ids") or [])[:10]:
            self.send_artifact(artifact_id, agent_id=message.get("agent_id"))

    def mirror_artifact(self, artifact_id):
        self.send_artifact(artifact_id)

    def send_artifact(self, artifact_id, force=False, agent_id=None):
        """Send one deliverable as a photo (images up to 10 MB), a document (up to 50 MB) or a note. Same file+version once."""
        if not self.linked() or (not force and not self.config()["forward"]["files"]):
            return "skipped"
        chat_id = self.chat_id()
        try:
            item = self.store.get("artifacts", artifact_id)
        except DomainError:
            return "missing"
        name = Path(str(item.get("path") or artifact_id)).name
        key = "tg:file:%s:%s" % (artifact_id, str(item.get("sha256") or "")[:12])
        if not force:
            wait = self.file_sent_at.get(artifact_id, 0) + FILE_COOLDOWN - time.time()
            if wait > 0:   # the same file changing again and again: send the newest version once the pause is over
                with self.lock:
                    self.jobs.append((time.time() + wait, "artifact", artifact_id))
                return "later"
            if not self.store.reserve_delivery(key):
                return "duplicate"
        self.file_sent_at[artifact_id] = time.time()
        caption = esc(item.get("title") or name)[:900]
        try:
            _, path = self.engine.outputs.resolve("artifact", artifact_id, verify=False)
            size = path.stat().st_size
        except (DomainError, OSError):
            self.say(chat_id, "%s is not available to send. Open it in AgentFlow." % name)
            return "missing"
        plan = file_plan(size, path.suffix)
        if plan == "note":
            self.say(chat_id, "%s (%.0f MB) is too large to send on Telegram. Open it in AgentFlow." % (name, size / 1048576))
            return "note"
        field, method = ("photo", "sendPhoto") if plan == "photo" else ("document", "sendDocument")
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self.enqueue(method, {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}, key="tg:file:" + artifact_id,
                     file={"field": field, "path": str(path), "name": name, "mime": mime}, ref={"t": "agent", "id": agent_id or item.get("agent_id") or "a-lead"})
        return plan

    def decision_kind(self, decision):
        return "permission" if (decision.get("permission_request") or decision.get("safety_request") or decision.get("command_request")
            or decision.get("browser_origin") or decision.get("kind") == "folder_access"
            or decision.get("action_handler") in ("browser", "browser_hostmap", "command", "safety")) else "question"

    def mirror_decision(self, decision_id):
        decision = self.store.get("decisions", decision_id)
        chat_id, forward = self.chat_id(), self.config()["forward"]
        if not chat_id or decision["status"] != "open" or decision.get("action_handler") == "telegram_link":
            return
        permission = self.decision_kind(decision) == "permission"
        if not (forward["permissions"] if permission else forward["questions"]):
            return
        # Coalesced requests share one decision (requesters[]); equal open questions are posted once as well.
        fingerprint = "|".join([decision.get("kind", ""), decision.get("question", ""), decision.get("path_key", "") or decision.get("requested_path", "")])
        posted = self.state().get("posted") or {}
        for other_id, other in posted.items():
            if other_id != decision_id and other.get("fp") == fingerprint and not other.get("answered"):
                try:
                    if self.store.get("decisions", other_id)["status"] == "open":
                        return
                except DomainError:
                    pass
        if not self.store.reserve_delivery("tg:decision:" + decision_id):
            return
        choices = [(a["label"], a["value"]) for a in (decision.get("actions") or [])] or [(o["label"], o["value"]) for o in decision.get("options") or []]
        lines = [self.who(decision.get("agent_id")) + (" needs permission" if permission else " asks"), esc(decision["question"][:1500])]
        if decision.get("context"):
            lines.append("<i>" + esc(self.vault.redact(decision["context"])[:900]) + "</i>")
        names = [self.name_of(r.get("agent_id")) for r in decision.get("requesters") or [] if r.get("agent_id")]
        if len(names) > 1 or (names and names[0] != self.name_of(decision.get("agent_id"))):
            lines.append("Requested by: " + esc(", ".join(dict.fromkeys(names))))
        lines.append("Tap an option, or reply to this message." if choices else "Reply to this message with your answer.")
        html = "\n\n".join(lines)
        keyboard = [[self.button(label, {"t": "dec", "id": decision_id, "label": label})] for label, value in choices[:8]]
        self.mutate(lambda s: (s.setdefault("posted", {}).__setitem__(decision_id, {"fp": fingerprint, "text": html, "chat_id": chat_id}), self.trim("posted", 120)))
        self.enqueue("sendMessage", {"chat_id": chat_id, "text": html, "parse_mode": "HTML", "disable_web_page_preview": True,
                                      **({"reply_markup": {"inline_keyboard": keyboard}} if keyboard else {})},
                     key="tg:decision:" + decision_id, ref={"t": "decision", "id": decision_id})

    def name_of(self, agent_id):
        try:
            return self.store.get("agents", agent_id)["name"]
        except DomainError:
            return "Agent"

    def mirror_answered(self, decision_id):
        self.edit_answered(decision_id)

    def edit_answered(self, decision_id):
        """Edit the posted question to its answered state and drop the buttons."""
        posted = (self.state().get("posted") or {}).get(decision_id)
        if not posted or not posted.get("message_id") or posted.get("answered"):
            return
        decision = self.store.get("decisions", decision_id)
        who = self.answered_by.pop(decision_id, "AgentFlow web")
        text = posted["text"] + "\n\n✅ <b>%s</b> — by %s" % (esc(str(decision.get("answer") or "answered")[:300]), esc(who))
        self.mutate(lambda s: s["posted"][decision_id].__setitem__("answered", True))
        self.enqueue("editMessageText", {"chat_id": posted["chat_id"], "message_id": posted["message_id"], "text": text[:TEXT_LIMIT], "parse_mode": "HTML",
                                          "reply_markup": {"inline_keyboard": []}}, key="tg:answered:" + decision_id)

    def test(self):
        """Settings > Telegram > Send test message."""
        if not self.token():
            raise DomainError("Save the bot token first.")
        if not self.linked():
            raise DomainError("No chat is linked yet. Send any message to the bot in your group; AgentFlow will ask you to approve the link.")
        if not self.threads:
            raise DomainError("Telegram is not running. Save the bot token again and check the status line.", 409)
        self.say(self.chat_id(), html=self.hello_html())
        return {"queued": True}

    # ---- restart notice ------------------------------------------------------------------------------------------------------------
    def note_restart(self, reason):
        self.remember(restart={"reason": str(reason)[:200], "at": now()})

    def announce_restart(self):
        """After a requested restart: one line in the web chat and in Telegram."""
        failure = restarter.read_failure(self.store.directory)
        pending = self.state().get("restart")
        if not pending and not failure:
            return
        self.remember(restart=None)
        username = self.state().get("username")
        if failure:   # the helper could not bring the runtime back; this start was done by hand
            restarter.clear_failure(self.store.directory)
            text = "The last AgentFlow restart did not complete (%s) Details: .agentflow/restart.log" % str(failure.get("reason") or "unknown reason").strip()[:300]
        else:
            took = self.restart_seconds(pending)
            text = "AgentFlow restarted (v%s%s)%s%s" % (__version__, self.commit_label(), " - took %ds" % took if took is not None else "",
                                                         " - Telegram connected as @" + username if username else "")
        try:
            self.engine.team.emit("a-lead", text, self.anchor_run(), "chat", telegram_skip=True)
        except DomainError:
            pass
        if self.linked():
            self.say(self.chat_id(), text)

    @staticmethod
    def restart_seconds(pending):
        """Seconds from the restart request to now, or None when the marker is stale or unreadable."""
        from datetime import datetime, timezone
        try:
            asked = datetime.fromisoformat(str((pending or {}).get("at")).replace("Z", "+00:00"))
            seconds = int((datetime.now(timezone.utc) - asked).total_seconds())
        except ValueError:
            return None
        return seconds if 0 <= seconds < 900 else None

    @staticmethod
    def commit_label():
        import subprocess
        try:
            out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=3)
            return "/" + out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else ""
        except (OSError, subprocess.SubprocessError):
            return ""
