"""Telegram chat commands (help, status, settings, goals, files, stop, approve, restart, unlink)."""
import re
from datetime import datetime, timezone

from . import config_schema
from .config_schema import ConfigError
from .store import DomainError

HELP = (
    "AgentFlow commands\n"
    "/status - what every member is doing, model, last activity, quota\n"
    "/agents - the team\n"
    "/files [n] - latest deliverables (tap to send)\n"
    "/goal <text> - add a goal for the team\n"
    "/stop [name|all] - stop work (asks to confirm)\n"
    "/approve ask|auto - approval mode for this chat\n"
    "/showall - every setting you can change\n"
    "/get <key> - show one setting\n"
    "/set <key> <value> - change a setting (/set <key> alone shows the choices)\n"
    "/restart - restart AgentFlow (asks to confirm)\n"
    "/unlink - unlink this chat (asks to confirm)\n"
    "\nPlain messages go to the default agent. Start with @Name or Name: to talk to another member. "
    "Reply to a question to answer it, or tap its buttons.")
STATUS_WORDS = {"queued": "Queued", "running": "Working", "waiting_user": "Waiting for you", "waiting_team": "Waiting for teammates",
                "blocked_model": "Needs a model", "blocked_account": "Needs sign-in", "waiting_quota": "Waiting for quota", "retrying": "Reconnecting",
                "review": "Reviewing", "ready": "Ready"}
STATUS_EMOJI = {"running": "🔵", "queued": "🟣", "retrying": "🟠", "waiting_user": "🟡", "blocked_model": "🟡", "blocked_account": "🟡",
                "waiting_quota": "🟡", "waiting_team": "🟣"}
TERMINAL = ("done", "failed", "cancelled")
ALIASES = {"start": "help", "show-all": "showall", "agent": "agents", "file": "files", "setting": "set", "config": "showall"}


class TelegramCommands:
    # ---- parsing -------------------------------------------------------------------------------------------
    def parse_command(self, text):
        """(name, args) for a /command addressed to this bot, ("ignore", "") for one addressed to another bot, else None."""
        match = re.match(r"^/([A-Za-z0-9_-]+)(?:@([A-Za-z0-9_]+))?(?:\s+(.*))?$", text or "", re.S)
        if not match:
            return None
        bot = str(self.state().get("username") or "").casefold()
        if match.group(2) and bot and match.group(2).casefold() != bot:
            return ("ignore", "")
        name = match.group(1).casefold()
        name = ALIASES.get(name, name.replace("-", "").replace("_", ""))
        if name not in ("help", "status", "showall", "get", "set", "agents", "goal", "files", "approve", "stop", "restart", "unlink", "answer"):
            return None   # /model, /goal-style chat commands and anything else go to the agent as a normal message
        return name, (match.group(3) or "").strip()

    def command(self, name, args, message, sender):
        chat_id = message["chat"]["id"]
        if name == "ignore":
            return "ignored"
        try:
            getattr(self, "cmd_" + name)(chat_id, args, sender)
        except ConfigError as exc:
            self.say(chat_id, str(exc) + ("\nChoices: " + ", ".join(v for v, _ in exc.options) if exc.options else ""))
        except DomainError as exc:
            self.say(chat_id, str(exc))
        return "command:" + name

    # ---- simple commands ---------------------------------------------------------------------------------------
    def cmd_help(self, chat_id, args, sender):
        self.say(chat_id, HELP)

    def cmd_agents(self, chat_id, args, sender):
        lines = []
        for agent in self.store.all("agents"):
            if agent.get("archived"):
                continue
            lines.append("%s %s - %s - %s" % (self.emoji(agent, self.active_run(agent["id"])), agent["name"], agent.get("role") or "member", self.model_of(agent, self.active_run(agent["id"]))))
        self.say(chat_id, "\n".join(lines) or "No teammates yet.")

    def cmd_goal(self, chat_id, args, sender):
        if not args:
            raise DomainError("Use /goal <what the team should achieve>.")
        result = self.engine.slash.goal({"goal": args, "channel": "dm-" + self.default_agent()})
        self.say(chat_id, "Goal added to project: " + result["project"]["title"])

    def cmd_answer(self, chat_id, args, sender):
        parts = args.split(None, 1)
        if len(parts) < 2:
            raise DomainError("Use /answer <question ID> <answer>.")
        from .telegram import display_name
        self.answer_decision(parts[0], parts[1], display_name(sender))

    def cmd_files(self, chat_id, args, sender):
        count = 5
        if args:
            try:
                count = config_schema.parse_number(args, True, 1, 10)
            except ConfigError:
                raise DomainError("Use /files or /files <1-10>.") from None
        items = [a for a in self.store.all("artifacts") if a.get("deliverable") and not a.get("external") and not a.get("archived")][-count:][::-1]
        if not items:
            self.say(chat_id, "No deliverables yet. Finished files from the team appear here.")
            return
        rows, lines = [], []
        for number, item in enumerate(items, 1):
            name = str(item.get("path") or item["id"]).replace("\\", "/").rsplit("/", 1)[-1]
            lines.append("%d. %s - %s" % (number, item.get("title") or name, self.name_of(item.get("agent_id"))))
            rows.append([self.button("Send " + name[:40], {"t": "file", "id": item["id"]})])
        self.say(chat_id, "\n".join(lines), keyboard=rows)

    # ---- status ---------------------------------------------------------------------------------------------------
    def active_run(self, agent_id):
        runs = [r for r in self.store.all("runs") if r["agent_id"] == agent_id and r["status"] not in TERMINAL and not r.get("system_managed")]
        busy = [r for r in runs if r["status"] == "running"]
        return (busy or runs or [None])[-1]

    def emoji(self, agent, run):
        if agent.get("status") == "paused":
            return "⏸"
        if run:
            return STATUS_EMOJI.get(run["status"], "🔵")
        return "⚪"

    def model_of(self, agent, run):
        try:
            binding = self.engine.models.effective(agent, run or {})
        except DomainError:
            binding = self.engine.default_binding(agent)
        return (binding.get("provider", "") + ":" + binding.get("model", "")) if binding.get("model") else "no model"

    def task_title(self, agent, run):
        if not run:
            return ""
        if run.get("task_id"):
            try:
                return self.store.get("tasks", run["task_id"])["title"]
            except DomainError:
                pass
        return (agent.get("current_action") or run.get("prompt") or "")[:90].replace("\n", " ")

    def status_html(self):
        from .telegram import esc, rel_time
        lines = []
        for agent in self.store.all("agents"):
            if agent.get("archived"):
                continue
            run = self.active_run(agent["id"])
            queued = sum(1 for r in self.store.all("runs") if r["agent_id"] == agent["id"] and r["status"] == "queued" and not r.get("system_managed"))
            stamps = [x for x in (run and run.get("last_activity_at"), run and run.get("updated_at"), agent.get("updated_at")) if x]
            state = "Paused" if agent.get("status") == "paused" else STATUS_WORDS.get(run["status"], "Working") if run else "Idle"
            task = self.task_title(agent, run)
            lines.append("%s <b>%s</b> - %s%s\n    %s | %s | queued %d" % (self.emoji(agent, run), esc(agent["name"]), state, (": " + esc(task)) if task else "",
                esc(self.model_of(agent, run)), esc("active " + rel_time(max(stamps)) if stamps else "no activity yet"), queued))
        quota = []
        for provider in ("codex", "claude"):
            try:
                windows = self.engine.providers.usage.public(provider).get("windows") or []
            except Exception:
                windows = []
            parts = []
            for window in windows:
                minutes = window.get("window_minutes")
                label = "5h" if minutes == 300 else "7d" if minutes == 10080 else str(window.get("label") or window.get("id") or "window")
                if label not in ("5h", "7d") and len(windows) > 2:
                    continue
                percent = window.get("used_percent")
                reset = window.get("resets_at")
                tail = ""
                if isinstance(reset, (int, float)) and not window.get("expired"):
                    minutes_left = max(0, int((reset - datetime.now(timezone.utc).timestamp()) // 60))
                    hours = round(minutes_left / 60)
                    tail = ", resets in " + (("%d d" % round(hours / 24)) if hours >= 48 else ("%d h" % hours) if minutes_left >= 90 else "%d min" % minutes_left)
                parts.append("%s %s%s" % (label, ("%d%% used" % round(percent)) if isinstance(percent, (int, float)) else "n/a", tail))
            if parts:
                quota.append("%s: %s" % (provider.capitalize() if provider == "codex" else "Claude", " | ".join(parts)))
        return "\n".join(lines or ["No teammates yet."]) + ("\n\n<b>Quota</b>\n" + "\n".join(esc(q) for q in quota) if quota else "")

    def cmd_status(self, chat_id, args, sender):
        self.say(chat_id, html=self.status_html())

    # ---- settings ----------------------------------------------------------------------------------------------------
    def cmd_showall(self, chat_id, args, sender):
        lines = []
        for group, rows in config_schema.listing(self.engine):
            lines.append("== " + group + " ==")
            lines += ["%s = %s  [%s]" % (key, shown, kind) for key, shown, kind in rows]
            lines.append("")
        lines.append("Change one: /set <key> <value>. Secrets are never shown or set here.")
        self.say(chat_id, "\n".join(lines))

    def cmd_get(self, chat_id, args, sender):
        field = config_schema.find(args.split(None, 1)[0] if args else "")
        lines = ["%s = %s" % (field.key, config_schema.show(config_schema.current(self.engine, field))), field.description, "Type: " + field.type_label()]
        if field.kind in ("selection", "bool"):
            options = [v for v, _ in config_schema.options_of(self.engine, field)] if field.kind == "selection" else ["on", "off"]
            lines.append("Choices: " + ", ".join(options))
        self.say(chat_id, "\n".join(lines))

    def option_keyboard(self, options, key):
        rows, row = [], []
        for value, label in options[:20]:
            row.append(self.button(label if len(label) < 28 else value[:28], {"t": "cfg", "key": key, "value": value}))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return rows

    def cmd_set(self, chat_id, args, sender):
        parts = args.split(None, 1)
        if not parts:
            raise DomainError("Use /set <key> <value>. /showall lists the keys.")
        try:
            field = config_schema.find(parts[0])
            if len(parts) == 1:
                if field.kind in ("selection", "model", "bool"):
                    options = [("on", "on"), ("off", "off")] if field.kind == "bool" else config_schema.options_of(self.engine, field)
                    shown = config_schema.show(config_schema.current(self.engine, field))
                    extra = "\nMore models: /set %s provider:model" % field.key if field.kind == "model" and len(options) > 20 else ""
                    self.say(chat_id, "%s (now %s)\nChoose a value:%s" % (field.key, shown, extra), keyboard=self.option_keyboard(options, field.key))
                    return
                raise ConfigError("%s needs a value (%s). Example: /set %s <value>" % (field.key, field.type_label(), field.key))
            value = config_schema.parse(self.engine, field, parts[1])
            old, new = config_schema.apply(self.engine, field, value)
        except ConfigError as exc:
            self.say(chat_id, str(exc) + ("\nChoices: " + ", ".join(v for v, _ in exc.options) if exc.options else ""))
            return
        self.say(chat_id, "%s: %s -> %s" % (field.key, old, new))

    def callback_config(self, payload, query, reply):
        try:
            field = config_schema.find(payload["key"])
            old, new = config_schema.apply(self.engine, field, config_schema.parse(self.engine, field, payload["value"]))
        except ConfigError as exc:
            reply(str(exc), True)
            return
        reply("Saved")
        self.edit_query(query, "%s: %s -> %s" % (field.key, old, new))

    # ---- confirmations ------------------------------------------------------------------------------------------------------
    def confirm(self, chat_id, text, action, **extra):
        yes = self.button("Confirm", {"t": "confirm", "action": action, **extra})
        no = self.button("Cancel", {"t": "confirm", "action": "cancel"})
        self.say(chat_id, text, keyboard=[[yes, no]])

    def cmd_stop(self, chat_id, args, sender):
        target = args.strip()
        if not target or target.casefold() == "all":
            self.confirm(chat_id, "Stop ALL active and queued work? Execution stays off until you turn it on again.", "stop", target="all")
            return
        agent = next((a for a in self.store.all("agents") if not a.get("archived") and target.casefold() in (a["name"].casefold(), a["name"].split()[0].casefold() if a["name"].split() else "", a["id"].casefold())), None)
        if not agent:
            raise DomainError("No teammate is called '%s'. /agents lists the team." % target[:40])
        self.confirm(chat_id, "Stop all work of %s?" % agent["name"], "stop", target=agent["id"])

    def cmd_approve(self, chat_id, args, sender):
        channel = config_schema.approval_channel(self.engine)
        mode = args.strip().casefold()
        if not mode:
            self.say(chat_id, "Approval mode for this chat: %s. Use /approve ask or /approve auto." % self.engine.approvals.modes().get(channel, "ask"))
            return
        if mode not in ("ask", "auto"):
            raise DomainError("Use /approve ask or /approve auto.")
        old = self.engine.approvals.modes().get(channel, "ask")
        self.engine.approvals.set(channel, mode)
        self.store.event("Approval mode changed from Telegram", "%s: %s -> %s" % (channel, old, mode), "settings")
        self.say(chat_id, "Approval mode: %s -> %s%s" % (old, mode, " (safe actions inside granted folders are approved without asking)" if mode == "auto" else ""))

    def cmd_restart(self, chat_id, args, sender):
        self.confirm(chat_id, "Restart AgentFlow now? Running work resumes from its checkpoints.", "restart")

    def cmd_unlink(self, chat_id, args, sender):
        self.confirm(chat_id, "Unlink this chat from AgentFlow? You can link it again later.", "unlink")

    def edit_query(self, query, text):
        message = query.get("message") or {}
        if message.get("message_id"):
            from .telegram import esc
            self.enqueue("editMessageText", {"chat_id": (message.get("chat") or {}).get("id"), "message_id": message["message_id"], "text": esc(text)[:4096],
                                             "parse_mode": "HTML", "reply_markup": {"inline_keyboard": []}})

    def callback_confirm(self, payload, query, reply):
        action = payload.get("action")
        if action == "cancel":
            reply("Cancelled")
            self.edit_query(query, "Cancelled.")
            return
        reply("Working")
        if action == "stop":
            if payload.get("target") == "all":
                self.engine.safety.stop_all()
                text = "Stopped all work. Execution is off: /set execution_enabled on resumes the team."
            else:
                runs = [r for r in self.store.all("runs") if r["agent_id"] == payload.get("target") and r["status"] not in TERMINAL]
                for run in runs:
                    self.engine.cancel(run["id"], cascade=True)
                text = "Stopped %d run%s of %s." % (len(runs), "" if len(runs) == 1 else "s", self.name_of(payload.get("target")))
        elif action == "restart":
            self.engine.request_restart("Telegram /restart")
            text = "Restarting AgentFlow. I will say when it is back."
        elif action == "unlink":
            self.unlink()
            text = "Unlinked. Messages from this chat are now ignored until you link it again."
        else:
            raise DomainError("This button has expired.")
        self.edit_query(query, text)
