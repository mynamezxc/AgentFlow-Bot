"""Owner approval mode ('ask' vs 'auto') and the decision handlers for browser/command requests.

'auto' = "Always approve for me": commands inside granted folders and browser/website access are approved
without a question and logged as one coalesced info message. Folder access, credentials and the AgentFlow
control plane are never auto-approved, and the existing command/secret protections still run.
"""
import threading
import time

from .store import DomainError

MODES = ("ask", "auto")
COALESCE_SECONDS = 60
MAX_ITEMS_SHOWN = 6


class ApprovalModes:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self.recent = {}  # owner channel -> {at, message_id, items}
        self.lock = threading.Lock()
        handlers = engine.decision_handlers
        handlers["browser"] = engine.browser.approval
        handlers["browser_hostmap"] = engine.browser.hostmap_approval
        handlers["command"] = engine.commands.approval

    # ---- mode -------------------------------------------------------------------------

    def modes(self):
        value = self.store.settings().get("approval_modes")
        return value if isinstance(value, dict) else {}

    def channel_for(self, run):
        """project.channel -> run.owner_channel -> run.channel."""
        project = None
        if run.get("project_id"):
            try:
                project = self.store.get("projects", run["project_id"])
            except DomainError:
                project = None
        return (project or {}).get("channel") or run.get("owner_channel") or run.get("channel") or ""

    def mode_for(self, run):
        mode = self.modes().get(self.channel_for(run), "ask")
        return mode if mode in MODES else "ask"

    def auto(self, run):
        return self.mode_for(run) == "auto"

    def set(self, channel, mode):
        channel = str(channel or "").strip()
        if not channel or len(channel) > 200:
            raise DomainError("Choose the conversation this approval mode applies to.")
        if mode not in MODES:
            raise DomainError("Approval mode must be ask or auto.")
        with self.store.transaction():
            modes = dict(self.modes())
            modes[channel] = mode
            self.store.settings({"approval_modes": modes})
        return {"channel": channel, "mode": mode}

    # ---- owner-visible log ----------------------------------------------------------------

    @staticmethod
    def render(items):
        """Plain one-line summary (no Markdown: commands contain backticks/newlines that break rendering)."""
        labels = [i["label"] if isinstance(i, dict) else str(i) for i in items]
        if len(labels) == 1:
            return "Auto-approved: " + labels[0]
        return "Auto-approved " + str(len(labels)) + " actions"

    @staticmethod
    def action(text, kind="other", detail=None, cwd=None):
        """Structured record the UI shows as one collapsed row (expand for the full command/target)."""
        label = " ".join(str(text).split())[:160]
        return {"type": kind, "label": label, "detail": str(detail if detail is not None else text)[:4000],
                **({"cwd": str(cwd)[:500]} if cwd else {}), "auto": True}

    def notify(self, run, text, kind="other", detail=None, cwd=None):
        """Log an auto-approval as a collapsed action row in the owner's chat (coalesced within 60 s).

        Audience "activity": it folds into the chat work log instead of a full message bubble."""
        channel = run.get("owner_channel") or run.get("channel") or "dm-a-lead"
        item = self.action(text, kind, detail, cwd)
        redact = self.engine.vault.redact
        item = {**item, "label": redact(item["label"]), "detail": redact(item["detail"])}
        with self.lock:
            entry = self.recent.get(channel)
            now = time.monotonic()
            if entry and now - entry["at"] < COALESCE_SECONDS:
                if item not in entry["items"]:
                    entry["items"].append(item)
                    try:
                        self.store.patch("messages", entry["message_id"], {"content": self.render(entry["items"]),
                                                                           "actions": entry["items"][-50:]})
                    except DomainError:
                        pass
                return entry["message_id"]
            message = self.engine.team.emit("a-lead", self.render([item]),
                {"id": run["id"], "channel": channel, "project_id": run.get("project_id")}, kind="action",
                audience="activity", actions=[item])
            self.recent[channel] = {"at": now, "message_id": message["id"], "items": [item]}
        outbound = run.get("owner_outbound") or run.get("outbound")
        if outbound and outbound.get("channel") == "telegram":
            self.engine.queue_reply(outbound, message["content"], "approval:" + message["id"])
        return message["id"]
