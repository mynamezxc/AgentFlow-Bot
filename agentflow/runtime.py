import hashlib
import json
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .channels import Channels
from .access import AccessManager
from .accounts import AccountUnavailable, ProviderLimit
from .models import ModelRouting, ModelUnavailable
from .collaboration import Collaboration
from .contract import INTERNAL_TOOLS, SYSTEM_PROMPT, TERMINAL, TOOLS, TOOL_SCHEMAS, WAITING, schema_error_detail
from .files import FileTools
from .research import fetch_page, public_page
from .schedule import next_schedule, parse_time
from .skills import SkillLibrary
from .plugins import PluginBroker
from .custom_tools import CustomTools
from .attachments import Attachments
from .progress import Progress
from .identity import face_for
from .outputs import Outputs
from .browser import BrowserTools
from .language import POLICY, foreign_ratio, language_name, owner_language
from .safety import RunSafety, LoopStopped
from .approvals import ApprovalModes
from .quota import QuotaManager, SHORT_DELAYS
from .commands import Commands
from .slash import Slash
from .goals import Goals
from .housekeeping import Housekeeping
from .heartbeat import Heartbeat
from .images import ImageEngines
from .mcp import McpManager
from .store import DomainError, now, uid
from .prompting import (IMAGE_TOKENS, PromptBuilder, arg_key, clip, clip_batch, estimate_tokens, gist, is_error, usage_record,
                        RESULT_LIMIT)
from .sessions import migrate_codex_usage
from .telegram import TelegramBridge
from .waitgraph import WaitGraph
from . import restarter


def structured(text):
    text = str(text).strip()
    fence = chr(96) * 3
    if text.startswith(fence):
        text = text.removeprefix(fence).removesuffix(fence).strip()
        if text.startswith("json"):
            text = text[4:].lstrip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {"reply": text}
    except ValueError:
        # Accept fenced/prose-wrapped envelopes from providers, never render raw JSON.
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", text):
            try:
                value, _ = decoder.raw_decode(text[match.start():])
                if isinstance(value, dict) and ("reply" in value or "tool_calls" in value):
                    return value
            except ValueError:
                continue
        return {"reply": text, "invalid_output": True}


class Engine(Channels):
    """Durable actor inboxes; resumable work; no human dispatch of routine work."""
    def __init__(self, store, providers, vault, workspace):
        self.store, self.providers, self.vault = store, providers, vault
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.guard = threading.RLock()
        # name -> callable(question, answer); used by runtime-routed decisions with `actions`.
        self.decision_handlers = {}
        self.stop = threading.Event()
        self.active = set()
        self.run_locks = {}
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="agentflow")
        self.thread = None
        self.last_heartbeat = None
        self.activity_marks = {}
        self.activity_lock = threading.Lock()
        self.files = FileTools(self)
        self.skills = SkillLibrary(store, self.workspace)
        self.team = Collaboration(self)
        self.plugins = PluginBroker(self)
        self.custom_tools = CustomTools(self)
        self.attachments=Attachments(self)
        self.progress=Progress(self)
        self.outputs=Outputs(self)
        self.browser=BrowserTools(self)
        self.access = AccessManager(self)
        self.safety = RunSafety(self)
        self.waits = WaitGraph(self)
        self.commands = Commands(self)
        self.migrate()
        self.models = ModelRouting(self)
        self.models.migrate()
        self.goals = Goals(self)
        self.goals.migrate()
        self.telegram = TelegramBridge(self)
        self.restart_requested = False
        self.on_restart = None  # server.py: graceful shutdown after a restart request
        self.spawn = subprocess.Popen
        for person in store.all("agents"):
            if not person.get("face"): store.patch("agents",person["id"],{"face":face_for(person["id"])})
        if not store.settings().get("capabilities_version"):
            atlas = store.get("agents", "a-lead")
            if atlas.get("role") == "Trưởng nhóm Marketing":
                store.patch("agents", "a-lead", {"role": 'General operator', "category": "general",
                    "description": 'Receive goals, organize the team and coordinate requested work.', "skills": []})
            store.settings({"capabilities_version": 1})
        for item in store.all("channel_outbox"):
            if item["status"] == "sending":
                store.patch("channel_outbox", item["id"], {"status": "unknown"})
        for run in store.all("runs"):
            if run["status"] in ("running", "queued") and run.get("contract_version") != 2:
                store.patch("runs", run["id"], {"status": "failed", "error": 'This older run has no checkpoint. Assign it again in chat.', "model_call_started_at": None})
                if run.get("task_id"):
                    store.patch("tasks", run["task_id"], {"status": "failed"})
            elif run["status"] == "running":
                store.patch("runs", run["id"], {"status": "queued", "recovered": True, "model_call_started_at": None})
            elif run.get("model_call_started_at"):
                store.patch("runs", run["id"], {"model_call_started_at": None})
        for post in store.all("posts"):
            if post["status"] == "publishing":
                store.patch("posts", post["id"], {"status": "unknown", "error": 'The runtime stopped while publishing. Reconcile on Facebook before retrying.'})
        for agent in store.all("agents"):
            if agent["status"] == "working":
                store.patch("agents", agent["id"], {"status": "idle", "current_action": ""})
        self.access.recover_blocked()
        migrate_codex_usage(self.store)  # one-time: Codex usage was cumulative per thread (see sessions.migrate_codex_usage)
        self.approvals = ApprovalModes(self)  # approval modes + browser/command/host-map decision handlers
        self.quota = QuotaManager(self)  # quota episodes, fallbacks and the 'quota' decision handler
        self.heartbeat = Heartbeat(self)  # loop cadence, active hours for schedules, token-frugal team pulse
        self.housekeeping = Housekeeping(self)  # disk cleanup, usage cadence hook and the 'cleanup' decision handler
        self.mcp = McpManager(self)  # owner-connected MCP servers, mcp.* broker tools and the 'mcp' decision handler
        self.slash = Slash(self)  # slash-command backends: /goal, /create-skill, /create-tool, /skill, skill catalog
        self.images = ImageEngines(self)  # image engines (Codex imagegen / Gemini API / gateway), settings `image`

    def migrate(self):
        settings = self.store.settings()
        if settings.get("autonomy_version", 0) >= 2:
            return
        agents = self.store.all("agents")
        primary = next((a for a in agents if a["id"] == "a-lead" and a.get("model")), None)
        primary = primary or next((a for a in agents if a.get("model")), None)
        binding = {"provider": primary.get("provider", "codex"), "model": primary.get("model", "")} if primary else {"provider": "codex", "model": ""}
        with self.store.transaction():
            self.store.settings({"autonomy_version": 2, "execution_enabled": True, "autonomy_enabled": True,
                "default_provider": binding["provider"], "default_model": binding["model"],
                "max_parallel": 4, "turns_per_slice": 6, "demo_data": False,
                "execution_image": "python:3.12-slim"})
            for actor in agents:
                changes = {"tools": sorted(set(actor.get("tools", [])) | TOOLS), "instructions": actor.get("instructions", "")}
                if not actor.get("model") and binding["model"]:
                    changes.update(binding, binding_origin="default")
                else:
                    changes["binding_origin"] = "owner" if actor.get("model") else "unconfigured"
                self.store.patch("agents", actor["id"], changes)
            for kind, identifiers in {
                "tasks": {f"t-{number}" for number in range(101, 108)},
                "posts": {"p-1", "p-2", "p-3", "p-4"},
                "messages": {"m-1", "m-2", "m-3"},
                "events": {"e-1", "e-2"},
            }.items():
                for item in self.store.all(kind):
                    if item["id"] in identifiers:
                        self.store.patch(kind, item["id"], {"sample": True})
            try:
                old = self.store.get("memories", "k-2")
                if "Atlas giao brief" in old.get("content", ""):
                    self.store.patch("memories", old["id"], {"archived": True})
            except DomainError:
                pass

    def default_binding(self, parent=None):
        settings = self.store.settings()
        if settings.get("default_model"):
            return {"provider": settings["default_provider"], "model": settings["default_model"],
                    **({"reasoning_effort": settings["default_reasoning_effort"]} if settings.get("default_reasoning_effort") else {})}
        return {"provider": (parent or {}).get("provider", "codex"), "model": (parent or {}).get("model", "")}

    def unstick(self, root_id):
        """Owner's Resume button: run the wait-cycle breaker for this root right now."""
        self.store.get("runs", root_id)
        resumed = self.waits.sweep(root_id=root_id, manual=True)
        return {"resumed": resumed, "message": ("Resumed %d run%s." % (len(resumed), "" if len(resumed) == 1 else "s")) if resumed else "No wait cycle found; everything is still progressing."}

    def start(self):
        try:
            self.waits.sweep(force=True)   # an existing cycle resolves right after a restart
        except Exception:
            self.store.event('Runtime needs attention', 'The wait-cycle check failed. Work and checkpoints are retained.', "error")
        self.thread = threading.Thread(target=self.loop, daemon=True, name="agentflow-inbox")
        self.thread.start()
        try:
            self.telegram.start()
        except Exception:
            self.store.event('Telegram did not start', 'The Telegram connection could not start. Check the bot token in Connections.', "error")

    def close(self):
        self.stop.set()
        self.telegram.close(1 if self.restart_requested else 3)
        for run_id in list(self.providers.processes):
            self.providers.cancel(run_id)
        if self.thread:
            self.thread.join(timeout=4)
        self.pool.shutdown(wait=True, cancel_futures=True)
        if getattr(self, 'mcp', None):
            self.mcp.close()
        self.browser.close()
        for thread in list(self.providers.status_threads.values()):
            thread.join(timeout=25)

    RESTART_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "Restart-AgentFlow.ps1"   # double-click / Stop path
    RESTART_HELPER = restarter.HELPER

    def request_restart(self, reason="", delay=2.0):
        """Restart the runtime: reply first, start the detached restart helper, then shut down quickly and for certain."""
        with self.guard:
            if self.restart_requested:
                return {"restarting": True, "already_requested": True}
            if os.name != "nt" and self.spawn is subprocess.Popen:
                raise DomainError("Self-restart is available on Windows. Restart the runtime from your service manager on this system.", 501)
            if not self.RESTART_HELPER.is_file():
                raise DomainError("The restart helper is missing: scripts/restart_helper.py.", 500)
            self.restart_requested = True
        self.store.event("AgentFlow restart requested", str(reason)[:200], "system")
        self.telegram.note_restart(reason)

        def go():
            deadline = time.monotonic() + 6
            while self.telegram.pending and time.monotonic() < deadline and not self.stop.is_set():
                time.sleep(0.2)   # let the "restarting" reply reach Telegram first
            time.sleep(delay)
            try:
                self.launch_restarter()
            except Exception as exc:
                self.restart_requested = False
                self.store.event("AgentFlow restart failed", type(exc).__name__ + ": the restart helper could not be started.", "error")
                return
            if self.on_restart:
                self.on_restart()
        threading.Thread(target=go, daemon=True, name="agentflow-restart").start()
        return {"restarting": True}

    def restart_command(self):
        """python restart_helper.py --pid .. -- <this interpreter and the original server argv>; the non-secret environment goes to a file."""
        from .service import SECRETS
        port = getattr(self, "http_port", None) or 8765
        env_file = restarter.write_env(self.store.directory / "restart-env.json", os.environ, SECRETS)
        return restarter.helper_command(os.getpid(), port, self.store.directory, env_file, restarter.server_command(port), helper=self.RESTART_HELPER)

    def launch_restarter(self, use_cim=None):
        """Start the helper outside this process tree: Win32_Process.Create through a one-line PowerShell, else a detached child."""
        command = self.restart_command()
        quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        if (os.name == "nt") if use_cim is None else use_cim:
            try:
                proc = self.spawn(restarter.powershell_command(restarter.cim_script(command, restarter.ROOT)), creationflags=no_window, **quiet)
                if proc.wait(timeout=30) == 0:
                    return proc
                self.store.event("AgentFlow restart helper", "WMI could not create the helper process; starting it directly.", "system")
            except Exception as exc:
                self.store.event("AgentFlow restart helper", type(exc).__name__ + " while using WMI; starting the helper directly.", "system")
        flags = 0
        if os.name == "nt":
            flags = no_window | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200) | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            try:
                return self.spawn(command, creationflags=flags | 0x01000000, cwd=str(restarter.ROOT), **quiet)   # breakaway from any job object
            except OSError:
                pass
        return self.spawn(command, creationflags=flags, cwd=str(restarter.ROOT), **quiet)

    def skill_instructions(self, actor):
        return sorted(({"id": s["id"], "name": s["name"], "instructions": s.get("content", "")[:6500]}
                       for s in self.store.all("skills") if s.get("installed") and s["id"] in actor.get("skills", [])), key=lambda s: s["id"])

    def runtime_guides(self):
        return [{'name': p.stem, 'instructions': p.read_text(encoding='utf-8')} for p in sorted((Path(__file__).parent / 'guides').glob('*.md'))]

    def context(self, actor, customer=False, run=None):
        """Compact work-tier context. The stable parts (identity, skills, guides) are also returned for
        callers that want them, but the prompt sends them once in the cached system prefix."""
        def bounded(items, count=6, limit=600):
            return [{k: (clip(v, limit) if k == "content" else v) for k, v in item.items()
                     if k not in ("absolute_path", "version")} for item in items[-count:]]
        memories = [m for m in self.store.all("memories") if not m.get("archived")]
        result = {"you": {k: actor.get(k) for k in ("id", "name", "role", "description", "instructions")},
                  "shared_knowledge": bounded([m for m in memories if m.get("scope") == "shared"])}
        if not customer:
            result["your_memory"] = bounded([m for m in memories if m.get("scope") == "private" and m.get("agent_id") == actor["id"]])
            result["team"] = [{k: clip(a.get(k), 200) if k in ("description", "current_action") else a.get(k)
                               for k in ("id", "name", "face", "color", "role", "parent_id", "category", "description", "status", "current_action")}
                              for a in self.store.all("agents") if not a.get("archived")]
            tasks = [t for t in self.store.all("tasks") if not t.get("archived") and not t.get("sample")
                     and (not run or t.get("project_id") == run.get("project_id"))]
            # Open work in full; only the newest finished tasks (tasks.inspect has the rest).
            finished = {t["id"] for t in tasks if t.get("status") in TERMINAL}
            newest = {t["id"] for t in tasks if t["id"] in finished}
            newest = set(sorted(newest, key=lambda i: next(n for n, t in enumerate(tasks) if t["id"] == i))[-8:])
            result["tasks"] = [{k: (clip(t.get(k), 400) if k == "result" else clip(t.get(k), 300) if k == "review_note" else t.get(k))
                                for k in ("id", "title", "agent_id", "reviewer_id", "status", "dependencies", "result", "review_note")}
                               for t in tasks if t["id"] not in finished or t["id"] in newest][-30:]
            profile = self.store.get("agents", actor["id"])
            result["your_model_binding"] = self.models.effective(profile, run)
            result["model_policy"] = profile.get("model_policy", {})
            result["available_models"] = [m["provider"] + ":" + m["id"] for m in self.models.catalog()]
            result["available_apps"] = {"plugins": [{k: p[k] for k in ("id", "name", "installed")} for p in self.plugins.catalog()],
                                        "accounts": self.plugins.accounts(actor), "operations": "plugins.list"}
            result["custom_tools"] = [{k: t.get(k) for k in ("id", "name", "description", "inputs")} for t in self.custom_tools.list()]
            result["browser_tools"] = self.browser.status()
            if run:
                result["pending_owner_decisions"] = [{k: q.get(k) for k in
                    ("id", "question", "project_id", "requested_path", "requested_access", "permission_request", "kind")}
                    for q in self.store.all("decisions") if q["status"] == "open" and q.get("channel") == run["channel"]]
            result["available_skills"] = [{k: s.get(k) for k in ("id", "name", "installed")} for s in self.store.all("skills") if not s.get("archived")][:60]
            if run and run.get("project_id"):
                steps = [c for c in self.progress.items(run["project_id"]) if not c.get("derived")]
                finished = [c["id"] for c in steps if c.get("status") in ("done", "skipped")][-8:]
                result["checklist"] = [{"id": c["id"], "title": c.get("title"), "status": c.get("status"), "agent_ids": c.get("agent_ids"),
                                        "note": clip(c.get("note"), 120), **({"task_id": c["task_id"]} if c.get("task_id") else {})}
                                       for c in steps if c.get("status") not in ("done", "skipped") or c["id"] in finished][-40:]
                project = self.store.get("projects", run["project_id"])
                result["project"] = {"id": project["id"], "title": project.get("title"), "brief": clip(project.get("brief"), 600),
                    "status": project.get("status"),
                    "active_goals": [{"id": g["id"], "title": clip(g.get("title"), 120), "text": clip(g.get("text"), 300)} for g in self.goals.active(project)],
                    "folder_grants": [{k: g.get(k) for k in ("id", "path", "target_type", "access")} for g in project.get("folder_grants", [])],
                    "browser_grants": [{k: g.get(k) for k in ("origin", "access")} for g in project.get("browser_grants", [])],
                    "context_paths": project.get("context_paths", [])[:12]}
                result["questions"] = [{k: clip(q.get(k), 300) if k in ("question", "answer") else q.get(k)
                                        for k in ("id", "question", "status", "answer", "agent_id")}
                                      for q in self.store.all("decisions") if q.get("project_id") == run["project_id"]][-8:]
                result["artifacts"] = [{k: a.get(k) for k in ("id", "title", "path", "agent_id", "kind", "is_image", "type")}
                                       for a in self.store.all("artifacts") if a.get("project_id") == run["project_id"]][-12:]
                result["file_changes"] = [{k:c.get(k) for k in ("id","artifact_id","path","agent_id","kind","added","deleted")}
                    for c in self.store.all("file_changes") if c.get("project_id")==run["project_id"]][-10:]
                result["recent_team_messages"] = [{**{k: m.get(k) for k in ("id", "agent_id", "target_id", "kind", "created_at")}, "content": clip(m.get("content"), 600)}
                    for m in self.store.all("messages") if m.get("project_id") == run["project_id"] and not m.get("sample")][-8:]
        result["skills"] = self.skill_instructions(actor)
        if not customer:
            result['runtime_guides'] = self.runtime_guides()
        return result

    def enqueue(self, agent_id, prompt, kind="chat", **metadata):
        actor = self.store.get("agents", agent_id)
        if metadata.get('root_id'):
            self.safety.before_child(metadata['root_id'], kind)
        if 'owner_language' not in metadata and metadata.get('root_id'):
            try:metadata['owner_language']=self.store.get('runs',metadata['root_id']).get('owner_language','en')
            except DomainError:pass
        if "active_skill_ids" not in metadata and metadata.get("root_id"):  # owner-selected /skill picks follow the whole task family
            try:
                picked=self.store.get("runs",metadata["root_id"]).get("active_skill_ids")
                if picked:metadata["active_skill_ids"]=list(picked)
            except DomainError:pass
        if "attachment_ids" not in metadata and metadata.get("root_id"):
            try: metadata["attachment_ids"]=self.store.get("runs",metadata["root_id"]).get("attachment_ids",[])
            except DomainError: pass
        if (actor.get("archived") or actor["status"] == "paused") and not (metadata.get('system_managed') and kind in ('access_approval','browser_approval','command_approval','safety_approval')):
            raise DomainError("The agent is paused or archived.", 409)
        record_id = uid("run")
        return self.store.create("runs", {"id": record_id, "agent_id": agent_id, "prompt": str(prompt)[:50000],
            "kind": kind, "status": "queued", "steps": 0, "trace": [], "phase": "model",
            "pending_calls": [], "wait_for_runs": [], "wait_for_tasks": [], "wait_for_decisions": [],
            "root_id": metadata.get("root_id") or record_id, "channel": "dm-" + agent_id,
            "contract_version": 2, "retry_count": 0, **metadata})

    def chat(self, data):
        content = str(data.get("message", "")).strip()
        attachment_ids=data.get("attachment_ids",[])
        if attachment_ids and not content: content='Read the attached files and help analyze their content.'
        if not content or len(content) > 20000:
            raise DomainError("Messages must contain 1 to 20,000 characters.")
        agent_id = data.get("agent_id", "a-lead")
        channel = data.get("channel", "dm-" + agent_id)
        if not isinstance(attachment_ids,list) or any(not isinstance(i,str) for i in attachment_ids):
            raise DomainError("Invalid attachment list.")
        self.store.get("agents", agent_id)
        if data.get("model_selection"):
            self.models.owner_select(agent_id, data["model_selection"])
        command = self.slash.command(data.get("command"))
        skill_ids = self.slash.skill_ids(data.get("skill_ids"))
        model_reply = None if command else self.models.owner_command(agent_id, content)
        with self.guard, self.store.transaction():
            project_id = data.get("project_id")
            if project_id:
                self.store.get("projects", project_id)
            else:
                project = next((p for p in reversed(self.store.all("projects")) if p.get("channel") == channel
                                and p["status"] in ("active", "waiting") and not p.get("archived")), None)
                if not project:
                    project = self.store.create("projects", {"title": content[:95], "brief": content, "goals": Goals.initial(content[:95], content),
                        "owner_agent_id": agent_id, "channel": channel, "status": "active", "folder_grants": []})
                project_id = project["id"]
            actor = self.store.get("agents", agent_id)
            self.attachments.bind(attachment_ids,project_id)
            inherited_files=next((m.get("attachment_ids") for m in reversed(self.store.all("messages"))
                if m.get("channel")==channel and m.get("project_id")==project_id and m.get("role")=="user" and m.get("attachment_ids")),[])
            selection = {"model_phase": "chat", "model_override": actor["chat_model_override"]} if actor.get("chat_model_override") else {}
            run = self.enqueue(agent_id, self.slash.wrap(command, content) if command else content, "chat", channel=channel, project_id=project_id, owner_channel=channel, attachment_ids=attachment_ids or inherited_files, owner_language=owner_language(content, self.channel_language(channel)), **self.slash.run_fields(command, skill_ids, content), **selection)
            self.store.create("messages", {"agent_id": agent_id, "role": "user", "content": content,
                "channel": channel, "project_id": project_id, "run_id": run["id"], "kind": "chat", "attachment_ids":attachment_ids,"audience":"owner",
                **({"command": command} if command else {}), **({"skill_ids": skill_ids} if skill_ids else {})})
            if model_reply is not None:
                self.team.emit(agent_id, model_reply, run)
                run = self.store.patch("runs", run["id"], {"status": "done", "result": model_reply, "completed_at": now()})
            return run

    def channel_language(self, channel):
        """Owner language of the latest owner message in this chat (None when unknown)."""
        for run in reversed(self.store.all("runs")):
            if run.get("channel") == channel and run.get("kind") == "chat" and run.get("owner_language"):
                return run["owner_language"]
        return None

    def path(self, name, write=False):
        return self.files.path(name)

    def tool(self, actor, call, run):
        name, args = call.get("name", ""), call.get("arguments", {})
        if name not in TOOLS or not isinstance(args, dict):
            raise DomainError("Invalid tool name or arguments.")
        if run.get("kind") == "support" and name not in ("chat.send", "memory.search"):
            raise DomainError("Customer messages cannot use administrative tools.", 403)
        if name not in actor.get("tools", []) and name not in ("tools.inspect", "tools.enable", "chat.send", "progress.update", "progress.set", "outputs.inspect", "files.request_access", "workspace.delete", "project.delete", "project.goals") and not name.startswith(("models.", "plugins.", "tools.", "mcp.")):
            raise DomainError("This tool is disabled. Enable internal tools through tools.enable: " + name)
        if name.startswith("models."):
            return self.models.invoke(name, args, actor, run)
        if name.startswith('browser.'):return self.browser.invoke(name,args,actor,run)
        if name=='outputs.register':return self.outputs.register(args,actor,run)
        if name=='outputs.view_image':return self.outputs.view_image(args,run)
        if name=='outputs.canvas':return self.outputs.canvas(args,actor,run)
        if name=="progress.set": return self.progress.set(args,actor,run)
        if name=="outputs.inspect":
            change=self.store.get("file_changes",args.get("change_id",""))
            if change.get("project_id")!=run.get("project_id"): raise DomainError("Only diffs for the current project can be read.",403)
            return self.files.change_view(change,args.get("offset",0))
        if name == "plugins.list":
            return {"plugins": self.plugins.catalog(), "accounts": self.plugins.accounts(actor)}
        if name == "plugins.invoke":
            return self.plugins.invoke(args, actor, run)
        if name.startswith("mcp."):
            return self.mcp.invoke(name, args, actor, run)
        if name == "tools.create":
            return self.custom_tools.save(actor, args, run)
        if name == "tools.update":
            return self.custom_tools.update(actor, args, run)
        if name == "tools.test":
            return self.custom_tools.test(actor, args, run)
        if name == "tools.archive":
            return self.custom_tools.archive(args, actor)
        if name == "tools.invoke":
            return self.custom_tools.invoke(actor, args, run)
        if name == "project.goals":
            return self.goals.invoke(args, actor, run)
        is_project = name.startswith("project.")
        if name in ("workspace.read", "project.read", "workspace.search", "project.search", "workspace.write", "project.write", "workspace.mkdir", "project.mkdir"):
            args, is_project, permission = self.access.prepare(actor, name, args, run)
            if permission:
                return permission
        if name in ("workspace.read", "project.read"):
            result = self.files.read(args, project=is_project)
            self.files.used(args,run,is_project)
            # Preserve the plain-text compatibility of workspace.read for a single file.
            plain = name == "workspace.read" and "content" in result and not args.get("offset") and not result.get("next_offset")
            return result["content"] if plain else result
        if name in ("workspace.search", "project.search"):
            result=self.files.search(args, project=is_project)
            self.files.used(args,run,is_project)
            return result
        if name in ("workspace.write", "project.write"):
            return self.files.write(args, actor, run, project=is_project)
        if name in ('workspace.mkdir','project.mkdir'):
            return self.files.mkdir(args,actor,run,project=is_project)
        if name in ('workspace.delete','project.delete'):
            if is_project:
                args, _, permission = self.access.prepare(actor, 'project.write', args, run)
                if permission:
                    return permission
            return self.files.delete(args, actor, run, project=is_project)
        if name == "workspace.execute":
            if args.get("project_id"):
                args, _, permission = self.access.prepare(actor, "project.read", args, run)
                if permission:
                    return permission
            return self.commands.execute(args, actor, run)
        if name == "web.fetch":
            return fetch_page(args, allow=lambda u: self.browser.covers_url(run, u), resolve=self.browser.resolver.resolve)
        if name == "image.generate":
            return self.images.tool(args,actor,run)
        if name == "content.draft":
            if not args.get("title") or not args.get("body") or args.get("channel", "blog") not in ("blog", "facebook"):
                raise DomainError("Drafts require a title, content and a valid channel.")
            return self.store.create("posts", {"title": str(args["title"])[:250], "body": str(args["body"])[:50000],
                "channel": args.get("channel", "blog"), "agent_id": actor["id"], "status": "draft",
                "media_type": "none", "media_url": "", "scheduled_at": "", "run_id": run["id"], "project_id": run.get("project_id")})
        if name == "plan.propose":
            return self.propose_plan(actor, args, run)
        return self.team.invoke(name, args, actor, run)

    def touch_run(self, run_id, gap=10):
        # Cheap liveness stamp for the UI ("last activity 30s ago"): at most one write per `gap` seconds per run.
        mark = time.monotonic()
        with self.activity_lock:
            if mark - self.activity_marks.get(run_id, -gap) < gap:
                return False
            self.activity_marks[run_id] = mark
        try:
            self.store.patch("runs", run_id, {"last_activity_at": now()})
        except DomainError:
            return False
        return True

    def begin_model_call(self, run_id):
        # The CLI is read without streaming, so a long turn is "thinking", not silence: the UI shows it while this is set.
        stamp = now()
        with self.activity_lock:
            self.activity_marks[run_id] = time.monotonic()
        try:
            self.store.patch("runs", run_id, {"model_call_started_at": stamp, "last_activity_at": stamp})
        except DomainError:
            pass

    def end_model_call(self, run_id):
        try:
            if self.store.get("runs", run_id).get("model_call_started_at"):
                self.store.patch("runs", run_id, {"model_call_started_at": None, "last_activity_at": now()})
        except DomainError:
            pass

    def execute_tool(self, actor, call, run, index):
        self.touch_run(run["id"])
        run = self.store.get("runs", run["id"])
        if run["status"] == "cancelled":
            return {"error": 'This work has stopped. No tool was executed.'}
        event_id = "tool-" + hashlib.sha256(f"{run['id']}:{run['steps']}:{index}".encode()).hexdigest()[:24]
        try:
            event = self.store.get("tool_events", event_id)
        except DomainError:
            event = None
        if event and event["status"] in ("done", "error"):
            return event["result"]
        if event and event["status"] == "running" and call["name"] in ("image.generate", "workspace.execute", "plugins.invoke", "tools.invoke", "tools.test", "browser.act", "mcp.call"):
            result = {"error": 'Execution was interrupted. The result is unconfirmed; inspect artifacts before retrying.', "unknown": True}
            self.store.patch("tool_events", event_id, {"status": "error", "result": result})
            return result
        if not event:
            self.store.create("tool_events", {"id": event_id, "agent_id": actor["id"], "run_id": run["id"],
                "project_id": run.get("project_id"), "channel": run["channel"], "name": call["name"],
                "arguments": json.loads(self.vault.redact(json.dumps(call.get("arguments", {}), ensure_ascii=False))),
                "status": "running", "step": run["steps"]})
        try:
            # Network/containers must not hold the SQLite writer lock.
            if call["name"].startswith('browser.') or call["name"] in ("web.fetch", "image.generate", "workspace.execute", "skills.install", "skills.research", "models.list", "plugins.invoke", "tools.invoke", "tools.test", "mcp.call"):
                result = self.tool(actor, call, run)
                self.store.patch("tool_events", event_id, {"status": "waiting_permission" if isinstance(result, dict) and result.get("waiting_permission") else "done", "result": result})
            else:
                with self.store.transaction():
                    result = self.tool(actor, call, run)
                    self.store.patch("tool_events", event_id, {"status": "waiting_permission" if isinstance(result, dict) and result.get("waiting_permission") else "done", "result": result})
            self.safety.tool_result(run, call, result)
            return self.dedupe_read(call, run, result)
        except LoopStopped:
            raise
        except (DomainError, KeyError, TypeError, ValueError, AttributeError) as exc:
            schema = schema_error_detail(call["name"])
            if not isinstance(exc, DomainError) and not schema:
                raise
            result = {"error": self.vault.redact(str(exc) if isinstance(exc, DomainError) else "Invalid arguments: " + type(exc).__name__ + " " + str(exc)[:200])}
            if schema and (not isinstance(exc, DomainError) or exc.status == 400):
                # Progressive disclosure: non-core tools have no schema in the prompt, so attach it here.
                result["schema"] = schema
            self.store.patch("tool_events", event_id, {"status": "error", "result": result})
            self.safety.tool_result(run, call, result)
            return result

    READ_TOOLS = ("workspace.read", "project.read")

    def read_key(self, call):
        """Identity of a file read for dedupe: (resolved path, size, mtime_ns, offset, limit) as a json string, else None."""
        fingerprint = getattr(self.files, "fingerprint", None)  # added by the tools agent; absent => no dedupe
        if not fingerprint or call.get("name") not in self.READ_TOOLS:
            return None
        args = call.get("arguments") or {}
        try:
            fp = fingerprint(args, call["name"].startswith("project."))
        except (DomainError, OSError, KeyError, TypeError, ValueError):
            return None
        if not fp:
            return None
        return json.dumps([str(fp[0]), fp[1], fp[2], args.get("offset") or 0, args.get("limit")], separators=(",", ":"))

    def dedupe_read(self, call, run, result):
        """A re-read of an unchanged file that is still in the CURRENT native session returns a stub, not the content.

        The map lives on `run.native_session.reads` ({key: turn}); it is only used while the session will continue
        (a session about to be compacted or reset keeps no promises) and starts empty with every new session."""
        if call.get("name") not in self.READ_TOOLS or is_error(result) or (isinstance(result, dict) and result.get("waiting_permission")):
            return result
        try:
            current = self.store.get("runs", run["id"])
            state = current.get("native_session") or {}
            if not state.get("id") or self.builder().will_reset(current):
                return result
            key = self.read_key(call)
            if not key:
                return result
            reads = dict(state.get("reads") or {})
            if key in reads:
                return {"unchanged": True, "path": (call.get("arguments") or {}).get("path", ""),
                        "note": f"Identical to your read at turn {reads[key]}; still in context."}
            if len(json.dumps(result, ensure_ascii=False, default=str)) <= RESULT_LIMIT:  # a clipped result is not "in context"
                reads[key] = state.get("turns", 0)
                for old in sorted(reads, key=reads.get)[:max(0, len(reads) - 200)]:
                    reads.pop(old)
                self.store.patch("runs", run["id"], {"native_session": {**state, "reads": reads}})
        except (DomainError, KeyError, TypeError, ValueError):
            pass
        return result

    def forget_reads(self, run_id, calls):
        """Drop dedupe entries for reads whose content did not make it into the turn (clipped to a gist)."""
        keys = {k for k in (self.read_key(c) for c in calls) if k}
        if not keys:
            return
        state = self.store.get("runs", run_id).get("native_session") or {}
        reads = state.get("reads") or {}
        if keys & set(reads):
            self.store.patch("runs", run_id, {"native_session": {**state, "reads": {k: v for k, v in reads.items() if k not in keys}}})

    def work_log(self, run, calls=(), note=None):
        """Bounded per-run log feeding the compact checkpoint: one line per tool call and a few progress notes."""
        log = run.get("work_log") or self.builder().derived_log(run.get("trace", []))
        notes = log.get("notes", [])
        return {"calls": (log.get("calls", []) + list(calls))[-80:], "notes": (notes + [note] if note else notes)[-40:]}

    def propose_plan(self, actor, data, run=None):
        run = run or {"id": uid("manual"), "channel": "dm-" + actor["id"], "project_id": None}
        tasks, subagents = data.get("tasks", []), data.get("subagents", [])
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= 100 or not isinstance(subagents, list):
            raise DomainError("The plan requires a valid task list.")
        # Validate the entire graph before creating any worker or task.
        aliases = [s.get("key", "") for s in subagents]
        if len(set(aliases)) != len(aliases) or any(not re.fullmatch(r"[a-zA-Z0-9_-]{1,60}", a) for a in aliases):
            raise DomainError("Invalid agent ID in the plan.")
        known = {a["id"] for a in self.store.all("agents")} | set(aliases)
        for child in subagents:
            self.models.child_config(actor, child)
        for index, task in enumerate(tasks):
            if not task.get("title") or task.get("agent_id") not in known:
                raise DomainError("Tasks require a title and an owner agent.")
            if task.get("reviewer_id") and (task["reviewer_id"] not in known or task["reviewer_id"] == task["agent_id"]):
                raise DomainError("The reviewer must differ from the task owner.")
            if any(not isinstance(d, int) or d < 0 or d >= index for d in task.get("depends_on", [])):
                raise DomainError("Dependencies must point to earlier work.")
        with self.store.transaction():
            mapping = {s["key"]: self.team.create_agent(actor, {k: v for k, v in s.items() if k != "key"}, run)["id"] for s in subagents}
            plan = self.store.create("plans", {"title": data.get("title", 'Implementation plan'), "description": data.get("description", ""),
                "agent_id": actor["id"], "project_id": run.get("project_id"), "tasks": tasks, "subagents": subagents, "status": "active"})
            created = []
            for task in tasks:
                created.append(self.team.delegate(actor, {**task, "agent_id": mapping.get(task["agent_id"], task["agent_id"]),
                    "reviewer_id": mapping.get(task.get("reviewer_id"), task.get("reviewer_id")),
                    "dependencies": [created[i]["task_id"] for i in task.get("depends_on", [])]}, run))
            return self.store.patch("plans", plan["id"], {"task_ids": [t["task_id"] for t in created]})

    def approve_plan(self, plan_id):
        plan = self.store.get("plans", plan_id)
        if plan["status"] != "proposed":
            raise DomainError("This plan has already been implemented.", 409)
        result = self.propose_plan(self.store.get("agents", plan["agent_id"]), plan)
        self.store.patch("plans", plan_id, {"status": "migrated", "replacement_id": result["id"]})
        return result

    def start_task(self, task_id):
        with self.guard, self.store.transaction():
            task = self.store.get("tasks", task_id)
            if task["status"] not in ("ready", "failed"):
                raise DomainError("This work is not ready or is already running.", 409)
            if any(self.store.get("tasks", dep)["status"] != "done" for dep in task.get("dependencies", [])):
                raise DomainError("Dependencies are not complete.", 409)
            prompt = task["title"] + "\n" + task.get("description", "") + "\nTASK_ID: " + task_id + '\nWhen the deliverable is ready, call tasks.submit with results and files, then finish the turn. The runtime initiates review. Do not wait for this task itself.'
            if task.get("review_note"):
                prompt += '\nREVISION FEEDBACK:\n' + task["review_note"]
            run = self.enqueue(task["agent_id"], prompt, "task", task_id=task_id, project_id=task.get("project_id"),
                root_id=task.get("root_id"), channel="dm-" + task["agent_id"], owner_channel=task.get("return_channel"),
                notify_agent_id=task.get("coordinator_id"), notify_channel=task.get("return_channel"),
                **task.get("model_selection", {}))
            self.store.patch("runs", run["id"], {"owner_outbound": task.get("owner_outbound")})
            self.store.patch("tasks", task_id, {"status": "running", "run_id": run["id"], "error": "", "last_review_run_id": None, "review_run_id": None})
            return run

    def review(self, task_id):
        with self.store.transaction():
            task = self.store.get("tasks", task_id)
            if task["status"] != "review" or not task.get("reviewer_id") or task["agent_id"] == task["reviewer_id"]:
                raise DomainError("This work is not ready for review.", 409)
            for run in self.store.all("runs"):
                if run.get("review_task_id") == task_id and run["status"] not in TERMINAL:
                    if run["status"] == "waiting_team":
                        return self.waits.resume_review(run, task)   # an earlier round's review run still waits: wake it for this draft
                    if task.get("review_run_id") != run["id"]:
                        self.store.patch("tasks", task_id, {"review_run_id": run["id"]})
                    return run
            run = self.enqueue(task["reviewer_id"], 'Review independently. Use tasks.review to approve/revise with a score and specific comments.'
                "\nTASK_ID: " + task_id + "\nBRIEF:\n" + task["title"] + "\n" + task.get("description", "")
                + '\nDRAFT:\n' + task.get("result", ""), "review", review_task_id=task_id,
                project_id=task.get("project_id"), root_id=task.get("root_id"), channel="dm-" + task["reviewer_id"],
                owner_channel=task.get("return_channel"), notify_agent_id=task.get("coordinator_id"), notify_channel=task.get("return_channel"),
                owner_outbound=task.get("owner_outbound"))
            self.store.patch("tasks", task_id, {"review_run_id": run["id"]})
            return run

    def waiting_status(self, run):
        decisions = [self.store.get("decisions", key) for key in run.get("wait_for_decisions", [])]
        if any(q["status"] == "open" and q["blocking"] for q in decisions):
            return "waiting_user"
        runs = [self.store.get("runs", key) for key in run.get("wait_for_runs", [])]
        tasks = [self.store.get("tasks", key) for key in run.get("wait_for_tasks", [])]
        if any(item["status"] not in TERMINAL for item in runs + tasks):
            return "waiting_team"
        return None

    def wake_waiters(self, check_accounts=True):
        self.resolve_review_cycles()
        self.waits.sweep()   # wait cycles / dead waits (throttled; also runs on every start)
        self.quota.sweep()  # end/advance quota episodes before deciding which waiting runs resume
        for run in self.store.all("runs"):
            if self.safety.halted(run):
                continue
            if run["status"] in ("waiting_user", "waiting_team") and not self.waiting_status(run):
                self.store.patch("runs", run["id"], {"status": "done" if run.get("system_managed") else "queued",
                    "resume_reason": 'An answer or teammate result is available.'})
            elif run["status"] == "blocked_model":
                actor = self.store.get("agents", run["agent_id"])
                binding = self.models.effective(actor, run)
                selected = {k: binding[k] for k in ("provider", "model")}
                available = any(m["provider"] == selected["provider"] and selected["model"] in (m["id"], m.get("resolved_model")) for m in self.providers.models)
                if binding.get("model") and (selected != run.get("blocked_binding") or available):
                    for question in self.store.all("decisions"):
                        if question.get("run_id") == run["id"] and question["kind"] == "model_config" and question["status"] == "open":
                            self.store.patch("decisions", question["id"], {"status": "answered", "answer": 'The workspace owner configured the model.', "answered_at": now()})
                    self.store.patch("runs", run["id"], {"status": "queued", "blocked_binding": None})
            elif run["status"] == "blocked_account" and check_accounts:
                actor = self.store.get("agents", run["agent_id"])
                actor = {**actor, **self.models.effective(actor, run)}
                try:
                    current = self.providers.accounts.check(actor["provider"])
                    if actor["provider"] not in ("codex", "claude", "gemini") or current.get("signed_in"):
                        self.store.patch("runs", run["id"], {"status": "queued", "error": "",
                            "resume_reason": 'Current account detected; resume from the same checkpoint.'})
                except DomainError:
                    pass
            elif run["status"] == "retrying" and run.get("retry_at", 0) <= time.time():
                self.store.patch("runs", run["id"], {"status": "queued"})
            elif run["status"] == "waiting_quota":
                # QuotaManager decides: episode ended/probe turn, model or account changed.
                if self.quota.should_resume(run):
                    self.store.patch("runs", run["id"], {"status": "queued", "error": ""})

    def resolve_review_cycles(self):
        """A natural peer review handoff can precede formal task submission."""
        for task in self.store.all("tasks"):
            if task["status"] != "running" or not task.get("run_id") or not task.get("reviewer_id"):
                continue
            worker = self.store.get("runs", task["run_id"])
            for peer_id in worker.get("wait_for_runs", []):
                peer = self.store.get("runs", peer_id)
                if (peer["status"] != "waiting_team" or peer["agent_id"] != task["reviewer_id"]
                        or task["id"] not in peer.get("wait_for_tasks", [])):
                    continue
                artifacts = [a for a in self.store.all("artifacts") if a.get("run_id") == worker["id"]]
                if not artifacts:
                    continue
                with self.store.transaction():
                    self.store.patch("tasks", task["id"], {"status": "review",
                        "result": worker.get("last_reply", "") + "\n" + "\n".join(a["path"] for a in artifacts),
                        "review_run_id": peer_id})
                    self.store.patch("runs", peer_id, {"status": "queued", "review_task_id": task["id"],
                        "wait_for_tasks": [key for key in peer.get("wait_for_tasks", []) if key != task["id"]],
                        "resume_reason": 'The owner handoff submitted the draft automatically. Continue with tasks.review.'})
                    self.store.event('Review wait cycle resolved', task["title"], "task")

    def builder(self):
        if getattr(self, "_prompt_builder", None) is None:
            self._prompt_builder = PromptBuilder(self)
        return self._prompt_builder

    def prompt(self, actor, run):
        """A `str` with the full legacy prompt plus the stable/volatile/delta split (see prompting.PromptBundle)."""
        return self.builder().build(actor, run)

    def native_patch(self, run, bundle, account, output):
        """Run fields to persist after a model turn: native session record, tier and token usage."""
        changes = {}
        if getattr(bundle, "tier", None) and bundle.tier != run.get("tier"):
            changes["tier"] = bundle.tier
        info = (account or {}).get("native")
        if info is not None and bundle.next_state is not None:
            previous = run.get("native_session") or {}
            resumed = bool(info.get("resumed"))
            sent = (set(previous.get("sent_images", [])) if resumed else set()) | set(info.get("image_keys", []))
            estimate = (previous.get("est_tokens", 0) if resumed else 0) + estimate_tokens(info.get("sent_chars", 0) + len(output or ""))
            estimate += IMAGE_TOKENS * len(info.get("image_keys", []))
            usage = usage_record((account or {}).get("usage")) or {}
            context = usage.get("context", 0)
            if context:  # the measured context of the last request (+ its answer) beats the character estimate
                estimate = max(estimate, context + usage.get("output", 0))
            seen = (account or {}).get("usage_seen") or (previous.get("usage_seen") if resumed else None)
            changes["native_session"] = ({
                "provider": bundle.provider, "model": bundle.model, "effort": bundle.effort, "id": info["session_id"],
                "turns": (previous.get("turns", 0) if resumed else 0) + 1, "system_digest": bundle.system_digest,
                "sent_images": sorted(sent), "context_digests": bundle.next_state["context_digests"],
                "context_records": bundle.next_state["context_records"], "marker": bundle.next_state["marker"],
                "last_message_at": bundle.next_state["last_message_at"], "est_tokens": estimate, "updated_at": now(),
                "last_context": context, "base_context": (previous.get("base_context", 0) if resumed else context),
                "reads": dict(previous.get("reads") or {}) if resumed else {},  # read-dedupe map: new session, new map
                **({"usage_seen": seen} if seen else {}),
                "resets": previous.get("resets", 0) + (0 if resumed or not previous else 1)} if info.get("session_id") else None)
        return changes

    def add_usage(self, run, actor_id, usage):
        """Accumulate provider-reported token usage on the run, its root and the agent (SPEC contract 1)."""
        if not usage:
            return
        total = sum(int(usage.get(k, 0) or 0) for k in ("input_tokens", "cache_creation_tokens", "output_tokens"))
        def add(record, key):
            current = record.get(key) or {}
            merged = {k: int(current.get(k, 0) or 0) + int(usage.get(k, 0) or 0)
                      for k in ("input_tokens", "cached_input_tokens", "cache_creation_tokens", "output_tokens")}
            return {**merged, "turns": int(current.get("turns", 0) or 0) + 1}
        with self.store.transaction():
            current = self.store.get("runs", run["id"])
            self.store.patch("runs", run["id"], {"token_usage": add(current, "token_usage")})
            root_id = current.get("root_id") or run["id"]
            try:
                root = self.store.get("runs", root_id)
                self.store.patch("runs", root_id, {"family_tokens": int(root.get("family_tokens", 0) or 0) + total,
                    "family_cached_tokens": int(root.get("family_cached_tokens", 0) or 0) + int(usage.get("cached_input_tokens", 0) or 0)})
            except DomainError:
                pass
            try:
                agent = self.store.get("agents", actor_id)
                self.store.patch("agents", actor_id, {"token_totals": add(agent, "token_totals")})
            except DomainError:
                pass

    def execute(self, run_id):
        with self.guard:
            lock = self.run_locks.setdefault(run_id, threading.Lock())
        if not lock.acquire(blocking=False):
            return
        try:
            run = self.store.get("runs", run_id)
            if run["status"] in TERMINAL or run["status"] in WAITING:
                return
            actor = self.store.get("agents", run["agent_id"])
            if not self.models.effective(actor, run).get("model"):
                self.team.ask(actor, {"question": 'Choose a default model in Atlas chat to start team work.',
                    "kind": "model_config", "blocking": True, "context": 'Agents inherit the default and can override it in private chat or switch by phase.'}, run)
                self.store.patch("runs", run_id, {"status": "blocked_model"})
                return
            self.store.patch("runs", run_id, {"status": "running", "started_at": run.get("started_at") or now()})
            slice_turns = min(24, max(1, int(self.store.settings().get("turns_per_slice", 6))))
            consumed = 0
            while consumed < slice_turns and not self.stop.is_set():
                run = self.store.get("runs", run_id)
                actor = self.store.get("agents", run["agent_id"])
                if run["status"] == "cancelled":
                    return
                if actor["status"] == "paused":
                    self.store.patch("runs", run_id, {"status": "queued"})
                    return
                if run.get("phase") == "tools":
                    results = []
                    for index, call in enumerate(run.get("pending_calls", [])):
                        if self.store.get("runs", run_id)["status"] == "cancelled":
                            return
                        result = self.execute_tool(actor, call, run, index)
                        if isinstance(result, dict) and result.get("waiting_permission"):
                            self.store.patch("runs", run_id, {"status": "waiting_user"})
                            return
                        results.append({"tool": call["name"], "result": result})
                    # Every result entering the turn is clipped (12k each, 40k per batch); tool_events keep the full result.
                    batch = clip_batch(results)
                    trace = run.get("trace", []) + [{"role": "tool", "content": json.dumps(batch, ensure_ascii=False)}]
                    calls_log = [{"s": run["steps"], "n": c["name"], "a": arg_key(c.get("arguments")), "r": gist(r["result"]), "e": is_error(r["result"])}
                                 for c, r in zip(run.get("pending_calls", []), results)]
                    self.store.patch("runs", run_id, {"trace": trace[-28:], "pending_calls": [], "phase": "model",
                                                      "work_log": self.work_log(run, calls_log)})
                    self.forget_reads(run_id, [c for c, r, b in zip(run.get("pending_calls", []), results, batch) if b["result"] is not r["result"]
                                               and not (isinstance(r["result"], dict) and r["result"].get("unchanged"))])
                    run = self.store.get("runs", run_id)
                    waiting = self.waiting_status(run)
                    if waiting:
                        self.store.patch("runs", run_id, {"status": waiting})
                        return
                actor = self.store.get("agents", run["agent_id"])
                selected = self.models.effective(actor, run)
                effort, adapted = self.builder().adaptive_effort(run, selected)
                actor = {**actor, "provider": selected["provider"], "model": selected["model"],
                         "reasoning_effort": effort, "input_images":self.attachments.images(run)+self.outputs.images(run) if run.get("kind")!="support" else []}
                self.safety.before_infer(run)
                bundle = self.prompt(actor, run)
                actor = self.builder().lite_effort(actor, bundle)
                self.begin_model_call(run_id)
                try:
                    output = self.providers.infer(actor, bundle, run_id)
                finally:
                    self.end_model_call(run_id)
                account = self.providers.account_runs.pop(run_id, None)
                history = run.get("model_history", []) + [{"step": run["steps"] + 1, **selected,
                    "effort_used": actor.get("reasoning_effort"), "usage": usage_record((account or {}).get("usage")),
                    "actual_models": (account or {}).get("actual_models", []), "at": now()}]
                self.store.patch("runs", run_id, {"model_history": history,
                    **({"account_fingerprint": account["fingerprint"]} if account else {}),
                    **self.native_patch(run, bundle, account, output)})
                self.add_usage(run, actor["id"], (account or {}).get("usage"))
                if self.store.get("runs", run_id)["status"] == "cancelled":
                    return
                consumed += 1
                response = structured(output)
                if response.get("invalid_output"):
                    failures = run.get("format_failures", 0) + 1
                    if failures > 2:
                        raise DomainError("The model did not return the expected tool protocol. Check the adapter. Work has not been marked complete.", 502)
                    self.store.patch("runs", run_id, {"format_failures": failures,
                        "trace": run.get("trace", []) + [{"role": "tool", "content": 'Return only JSON {reply,state,tool_calls}. The parent executes broker tools. Do not call native tools.'}]})
                    continue
                calls = response.get("tool_calls") or []
                if not isinstance(calls, list) or len(calls) > 24 or any(
                        not isinstance(c, dict) or not isinstance(c.get("name"), str) or not isinstance(c.get("arguments", {}), dict) for c in calls):
                    raise DomainError("The model returned an invalid tool structure.", 502)
                step = run["steps"] + 1
                reply = str(response.get("reply", ""))
                messages = response.get("messages", [])
                if not isinstance(messages, list):
                    messages = []
                trace = run.get("trace", []) + [{"role": "assistant", "content": json.dumps(response, ensure_ascii=False)[:45000]}]
                attached = self.envelope_files(run, actor, response, calls)
                if attached["notes"]:
                    trace.append({"role": "tool", "content": json.dumps([{"tool": "attachments", "result": {"error": "Some files were not attached.", "details": attached["notes"]}}], ensure_ascii=False)[:4000]})
                digest = hashlib.sha256(json.dumps(response, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                repeated = run.get("repeated_output", 0) + 1 if digest == run.get("last_output_hash") else 0
                with self.store.transaction():
                    note = str(response.get("progress") or reply or "").strip()
                    self.store.patch("runs", run_id, {"steps": step, "trace": trace[-28:], "phase": "tools" if calls else "model",
                        "work_log": self.work_log(run, note={"s": step, "t": clip(note, 200)} if note else None),
                        "pending_calls": calls, "last_output_hash": digest, "repeated_output": repeated,
                        "last_reply": reply or run.get("last_reply", ""), "retry_count": 0, "pending_files": attached["pending"],
                        **({"summary": str(response["summary"]).strip()[:1500]} if str(response.get("summary") or "").strip() else {}),
                        "provider": actor["provider"], "model": actor["model"]})
                    if response.get("progress"):
                        self.store.patch("agents", actor["id"], {"current_action": str(response["progress"])[:180]})
                    texts = ([reply] if reply.strip() else []) + [str(v.get("text", "") if isinstance(v, dict) else v) for v in messages]
                    spoken = [i for i, text in enumerate(texts) if text.strip()]
                    kind = "progress" if calls or response.get("state") == "continue" else "chat"
                    for index, text in enumerate(texts):
                        if text.strip():
                            extra = {"artifact_ids": attached["ids"]} if attached["ids"] and index == spoken[-1] else {}
                            # Owner chat shows only final answers, chat.send, decisions and attachments; intermediate
                            # step narration is kept as a collapsed work log (audience "activity"), never pushed to Telegram.
                            if kind == "progress" and not extra:
                                extra = {"audience": "activity"}
                            elif run.get("system_pulse") and run.get("pulse_notify") != "always":
                                extra = {**extra, "audience": "activity"}  # silent pulse: owner hears only via chat.send / decisions
                            self.team.emit(actor["id"], text, run, kind, step=step, **extra)
                    if attached["ids"] and not spoken:
                        self.team.emit(actor["id"], "Attached: " + ", ".join(self.store.get("artifacts", i)["title"] for i in attached["ids"]),
                                       run, kind, step=step, artifact_ids=attached["ids"])
                if repeated >= 3:
                    self.safety.halt(run,'The agent repeated the same response four times. This branch stopped to prevent quota waste.')
                if calls:
                    self.store.patch('runs',run_id,{'text_only_turns':0})
                    continue
                if response.get('state')=='continue':
                    idle_turns=run.get('text_only_turns',0)+1
                    self.store.patch('runs',run_id,{'text_only_turns':idle_turns})
                    if idle_turns>=12:
                        self.safety.halt(run,'The agent sent 12 updates without tools or deliverables. Work stopped to prevent a loop.')
                run = self.store.get("runs", run_id)
                waiting = self.waiting_status(run)
                if waiting:
                    self.store.patch("runs", run_id, {"status": waiting})
                    return
                if response.get("state") == "continue":
                    continue
                if response.get("state") == "waiting":
                    # A textual promise to wait is insufficient: wait for actual outstanding work.
                    pending = [t for t in self.store.all("tasks") if t.get("project_id") == run.get("project_id")
                               and t.get("coordinator_id") == actor["id"] and t["status"] not in TERMINAL]
                    if pending:
                        self.store.patch("runs", run_id, {"status": "waiting_team", "wait_for_tasks": [t["id"] for t in pending]})
                        return
                self.finish(run, actor, reply or run.get("last_reply", ""))
                return
            if self.store.get("runs", run_id)["status"] not in TERMINAL:
                self.store.patch("runs", run_id, {"status": "queued", "yielded_at": now()})
        except Exception as exc:
            run = self.store.get("runs", run_id)
            if run["status"] == "cancelled":
                return
            if isinstance(exc, LoopStopped):
                # A domain tool may have rolled back its outer transaction.
                # Persist the brake after leaving it, before the next pump.
                try:self.safety.halt(run,str(exc))
                except LoopStopped:pass
                return
            if self.stop.is_set():
                self.store.patch("runs", run_id, {"status": "queued"})
                return
            if isinstance(exc, AccountUnavailable):
                self.store.patch("runs", run_id, {"status": "blocked_account", "error": str(exc)})
                self.team.emit(run["agent_id"], str(exc), run, "error")
                return
            if isinstance(exc, ProviderLimit):
                # One quota episode per provider/account: park this run (checkpoint kept), notify the owner once,
                # and let pump() hold teammates on the same account without spawning more CLIs.
                self.quota.on_limit(run, exc)
                return
            if isinstance(exc, ModelUnavailable):
                selected = self.models.effective(self.store.get("agents", run["agent_id"]), run)
                self.team.ask(self.store.get("agents", run["agent_id"]), {"question": str(exc),
                    "kind": "model_config", "blocking": True}, run)
                self.store.patch("runs", run_id, {"status": "blocked_model", "error": str(exc),
                    "blocked_binding": {k: selected[k] for k in ("provider", "model")}})
                return
            message = self.vault.redact(str(exc)) if isinstance(exc, DomainError) else 'The runtime did not complete this turn. Check the connection or retry.'
            retries = run.get("retry_count", 0) + 1
            transient = isinstance(exc, DomainError) and exc.status in (429, 502, 503, 504, 529)
            if transient and retries <= len(SHORT_DELAYS):
                # Short rate limits / overload: 30s -> 2m -> 5m, then a clear failure.
                self.store.patch("runs", run_id, {"status": "retrying", "retry_count": retries, "error": message,
                    "retry_at": time.time() + SHORT_DELAYS[retries - 1]})
            else:
                if transient:
                    message = ('The provider kept rate-limiting or was overloaded after ' + str(len(SHORT_DELAYS)) + ' retries (30 seconds, 2 minutes, 5 minutes). '
                               'The work keeps its checkpoint. Try again later or choose another model or provider. Last error: ' + message)
                self.store.patch("runs", run_id, {"status": "failed", "error": message, "completed_at": now()})
                self.team.emit(run["agent_id"], message, run, "error")
                if run.get("task_id"):
                    self.store.patch("tasks", run["task_id"], {"status": "failed", "error": message})
                self.notify(run, message)
        finally:
            actor_id = self.store.get("runs", run_id)["agent_id"]
            current = self.store.get("agents", actor_id)
            run = self.store.get("runs", run_id)
            self.store.patch("agents", actor_id, {"status": "paused" if current["status"] == "paused" else "idle",
                "current_action": 'Waiting for your decision' if run["status"] in ("waiting_user", "blocked_model") else
                                  'Waiting for current account sign-in' if run["status"] == "blocked_account" else
                                  self.quota.action_text(run) if run["status"] == "waiting_quota" else
                                  'Waiting for teammate results' if run["status"] == "waiting_team" else "",
                "last_active": now()})
            with self.guard:
                self.active.discard(actor_id)
            lock.release()

    def envelope_files(self, run, actor, response, calls):
        """Envelope `files`: attach existing workspace/project files to this turn's owner message.

        With pending tool calls the files may not exist yet, so they wait for the next tool-free turn."""
        if run.get("kind") == "support":
            return {"ids": [], "notes": [], "pending": []}  # customer-facing runs never attach workspace files
        raw = response.get("files")
        wanted = [str(f) for f in (run.get("pending_files") or [])]
        wanted += [str(f) for f in ([raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []) if isinstance(f, str)]
        wanted = list(dict.fromkeys(wanted))[:10]
        if not wanted or calls:
            return {"ids": [], "notes": [], "pending": wanted}
        result = self.outputs.attach(wanted, actor, run)
        return {"ids": result["artifact_ids"], "notes": result["notes"], "pending": []}

    def finish(self, run, actor, reply):
        permission = self.access.escalate_blocked(actor, run, reply)
        if permission and permission.get("waiting_permission"):
            self.store.patch("runs", run["id"], {"status": "waiting_user", "resume_reason": 'Waiting for an Atlas access decision.'})
            return
        if permission and permission.get("status") == "granted":
            repairs = run.get("access_repair_count", 0) + 1
            if repairs > 2:
                raise DomainError("Access is granted but the agent has not read the source. Work is incomplete; use the file broker.", 409)
            self.store.patch("runs", run["id"], {"status": "queued", "access_repair_count": repairs,
                "resume_reason": 'Project access is granted. Use project.read or workspace.read with the actual path through the broker, rather than native CLI access.'})
            return
        with self.guard, self.store.transaction():
            current = self.store.get("runs", run["id"])
            if current["status"] == "cancelled":
                return
            if not reply.strip():
                reply = 'Work processed. Details are in the updates and deliverables above.'
            finished_task = None
            self.store.patch("runs", run["id"], {"status": "done", "result": reply, "error": None, "completed_at": now()})
            if run.get("task_id"):
                task = self.store.get("tasks", run["task_id"])
                if task["status"] == "running":
                    self.store.patch("tasks", task["id"], {"status": "review" if task.get("auto_review", True) and task.get("reviewer_id") else "done", "result": reply})
                    finished_task = task["id"] if task.get("auto_review") and task.get("reviewer_id") else None
            if run.get("review_task_id"):
                task = self.store.get("tasks", run["review_task_id"])
                if task["status"] == "review":
                    self.store.patch("tasks", task["id"], {"review_feedback": reply, "review_run_id": None})
                    self.team.ask(actor, {"question": 'Review comments are available but no approve/revise decision was recorded. How should we proceed?',
                        "context": reply[:4000], "blocking": False}, run)
            if not run.get("system_pulse"):  # routine pulses must not flood the agent's episode memory
                memory_title, memory_content = self.episode_memory(run, reply)
                self.store.create("memories", {"title": memory_title, "content": memory_content,
                    "scope": "private", "agent_id": actor["id"], "type": "episode", "project_id": run.get("project_id"), "tags": []})
        if finished_task:
            try:
                self.review(finished_task)   # the one path that also wakes an older review run still waiting for this task
            except DomainError:
                pass
        self.notify(run, reply)
        if run.get("outbound") and run["outbound"]["channel"] != "telegram":
            self.send_reply(run["outbound"], reply)
        self.wake_waiters()

    def episode_memory(self, run, reply):
        """Memory is English: prefer the model's English `summary`; never store owner-language prose as a lesson."""
        current = self.store.get("runs", run["id"])
        summary = current.get("summary") or ""
        task = self.store.get("tasks", run["task_id"]) if run.get("task_id") else {}
        subject = task.get("title") or run["prompt"]
        if foreign_ratio(subject) > 0.15:
            subject = (run.get("kind") or "work").capitalize() + " run " + run["id"]
        if summary and foreign_ratio(summary) <= 0.15:
            content = summary
        elif foreign_ratio(reply) <= 0.15:
            content = reply[:3000]
        else:
            files = [a["path"] for a in self.store.all("artifacts") if a.get("run_id") == run["id"]][:10]
            content = ("Completed; the owner-facing report is in " + language_name(run.get("owner_language")) +
                       " (run " + run["id"] + "). No English summary was provided." + (" Files: " + ", ".join(files) if files else ""))
        return "Work: " + subject[:120], content

    def notify(self, run, reply):
        summary = self.store.get("runs", run["id"]).get("summary")
        # Teammates coordinate in English: hand back the English summary when the reply targets the owner's language.
        if summary and foreign_ratio(reply) > 0.15:
            reply = summary
        target = run.get("reply_to_agent_id") or run.get("notify_agent_id")
        if not target or target == run["agent_id"]:
            return
        key = "notify:" + run["id"]
        with self.store.transaction():
            if not self.store.reserve_delivery(key):
                return
            channel = run.get("reply_to_channel") or run.get("notify_channel") or "dm-" + target
            self.team.emit(run["agent_id"], reply, {**run, "channel": channel}, "peer_reply",
                           target_id=target, related_run_id=run["id"], handoff_event_id=run.get("handoff_event_id"))
            self.wake_waiters(check_accounts=False)
            pending = [r for r in self.store.all("runs") if r["agent_id"] == target and r.get("project_id") == run.get("project_id")
                       and r["status"] not in TERMINAL]
            if pending:
                return
            # Coalesce callbacks already in the inbox rather than creating an endless ping-pong.
            callback = self.enqueue(target, 'Teammate ' + self.store.get("agents", run["agent_id"])["name"]
                    + ' returned a result. Read the context, continue coordination and report conclusions to the owner.\n' + reply[:18000],
                    "callback", project_id=run.get("project_id"), root_id=run.get("root_id"), channel=channel,
                    received_run_id=run["id"], owner_outbound=run.get("owner_outbound"),
                    outbound=run.get("owner_outbound") if channel.startswith("telegram:") else None)

    def cancel(self, run_id, cascade=False):
        with self.guard:
            root = self.store.get("runs", run_id)
            targets = [r for r in self.store.all("runs") if r["id"] == run_id or
                       (cascade and r.get("root_id") == root.get("root_id"))]
            for run in targets:
                if run["status"] in TERMINAL:
                    continue
                self.store.patch("runs", run["id"], {"status": "cancelled", "completed_at": now(), "model_call_started_at": None})
                self.providers.cancel(run["id"])
                for q in self.store.all("decisions"):
                    if q.get("run_id") == run["id"] and q["status"] == "open":
                        self.store.patch("decisions", q["id"], {"status": "cancelled"})
                if run.get("task_id"):
                    self.store.patch("tasks", run["task_id"], {"status": "cancelled"})
            return self.store.get("runs", run_id)

    def dispatch_tasks(self):
        for task in self.store.all("tasks"):
            if task.get("archived") or task.get("sample"):
                continue
            if task.get('root_id') and self.safety.halted(self.store.get('runs',task['root_id'])):
                continue
            if task["status"] == "ready" and task.get("auto_run"):
                try:
                    self.start_task(task["id"])
                except DomainError:
                    pass
            elif task["status"] == "review" and task.get("auto_review") and not task.get("review_run_id") and not task.get("last_review_run_id"):
                try:
                    self.review(task["id"])
                except DomainError:
                    pass

    def tick(self, current=None):
        current = current or datetime.now(timezone.utc)
        self.last_heartbeat = now()
        self.housekeeping.tick()
        if not self.store.settings().get("execution_enabled", True):
            return
        beat = self.heartbeat.config()
        quiet = self.heartbeat.quiet(current, beat)  # quiet hours pause scheduled work only, never owner chat or running work
        for schedule in self.store.all("schedules"):
            if not schedule.get("enabled") or schedule.get("archived"):
                continue
            actor = self.store.get("agents", schedule["agent_id"])
            if not actor.get("heartbeat") or actor["status"] == "paused":
                continue
            if not schedule.get("next_run"):
                self.store.patch("schedules", schedule["id"], {"next_run": next_schedule(schedule, current)})
            elif parse_time(schedule["next_run"]) <= current:
                if quiet and self.heartbeat.hold(schedule, current, beat):
                    continue  # fires once when the window opens, never as a backlog burst
                with self.store.transaction():
                    if self.store.reserve_delivery("schedule:" + schedule["id"] + ":" + schedule["next_run"]):
                        self.enqueue(schedule["agent_id"], schedule["prompt"], "schedule", schedule_id=schedule["id"],
                                     channel="dm-" + schedule["agent_id"])
                        self.store.patch("schedules", schedule["id"], {"last_run": now(), "next_run": next_schedule(schedule, current)})
        for post in self.store.all("posts"):
            if post["status"] == "approved" and post.get("scheduled_at") and parse_time(post["scheduled_at"]) <= current and post.get("channel") == "facebook":
                self.publish(post["id"])
        self.heartbeat.pulse_tick(current)

    def heartbeat_status(self):
        return self.heartbeat.status()

    def pump(self):
        if not self.store.settings().get("execution_enabled", True):
            return []
        self.wake_waiters()
        self.dispatch_tasks()
        self.dispatch_outbox()
        submitted = []
        limit = min(8, max(1, int(self.store.settings().get("max_parallel", 4))))
        for run in self.store.all("runs"):
            if run["status"] != "queued":
                continue
            if self.safety.halted(run):
                continue
            if self.quota.gate(run):
                continue  # provider/account is paused by a quota episode: parked without spawning a CLI
            with self.guard:
                if run["agent_id"] in self.active or len(self.active) >= limit:
                    continue
                actor = self.store.get("agents", run["agent_id"])
                if actor["status"] == "paused" or actor.get("archived"):
                    continue
                self.active.add(actor["id"])
                self.store.patch("runs", run["id"], {"status": "running"})
                self.store.patch("agents", actor["id"], {"status": "working", "current_action": 'Processing request'})
                submitted.append(self.pool.submit(self.execute, run["id"]))
        return submitted

    def loop(self):
        last_tick = None
        while not self.stop.wait(0.5):
            try:
                if last_tick is None or time.monotonic() - last_tick >= self.heartbeat.interval():
                    last_tick = time.monotonic()
                    self.tick()
                self.pump()
            except Exception:
                self.store.event('Runtime needs attention', 'A coordination cycle failed. Work and checkpoints are retained.', "error")
