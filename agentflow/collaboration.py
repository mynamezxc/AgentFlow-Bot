import hashlib
import json
import re
from pathlib import Path

from .contract import INTERNAL_TOOLS, TERMINAL, TOOLS, TOOL_SCHEMAS, inspect_tools
from .authoring import check_agent_text
from .language import require_english
from .store import DomainError, now, uid
from .goals import Goals
from .identity import face_for, FACES, COLORS


class Collaboration:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store

    def emit(self, agent_id, content, run, kind="chat", **extra):
        audience=extra.pop("audience",None) or ("internal" if kind in ("peer","peer_reply","handoff") or run.get("kind")=="peer" and kind!="decision" else "owner")
        message = self.store.create("messages", {
            "agent_id": agent_id, "role": "assistant", "content": self.engine.vault.redact(str(content))[:50000],
            "channel": run.get("channel", "dm-" + agent_id), "project_id": run.get("project_id"),
            "run_id": run["id"], "kind": kind, "audience":audience, **extra})
        self.engine.touch_run(run["id"])
        outbound = run.get("outbound")
        if audience=="owner" and kind in ("chat", "progress", "error") and outbound and outbound.get("channel") == "telegram":
            self.engine.queue_reply(outbound, content, "message:" + message["id"], artifact_ids=extra.get("artifact_ids"))
        return message

    def create_agent(self, actor, args, run):
        if any(k in args for k in ("default_model", "credentials")):
            raise DomainError("Accounts, credentials and team defaults require an actual owner request.")
        name, role = str(args.get("name", "")).strip(), str(args.get("role", "")).strip()
        if not name or not role or len(name) > 120:
            raise DomainError("New agents require a specific name and role.")
        check_agent_text(args)
        existing = next((a for a in self.store.all("agents") if not a.get("archived") and a["name"].casefold() == name.casefold()), None)
        if existing:
            return {"id": existing["id"], "name": existing["name"], "existing": True}
        settings = self.store.settings()
        if len([a for a in self.store.all("agents") if not a.get("archived")]) >= int(settings.get("max_agents", 128)):
            raise DomainError("The workspace agent limit has been reached. Refine or reuse an existing teammate.")
        tools = args.get("tools") or INTERNAL_TOOLS
        if not isinstance(tools, list) or set(tools) - TOOLS:
            raise DomainError("This tool is unavailable in the runtime. Use tools.inspect to choose an available tool.")
        binding = self.engine.models.child_config(actor, args)
        agent_id=uid("a")
        if args.get("face") and args["face"] not in FACES or args.get("color") and args["color"] not in COLORS:
            raise DomainError("Choose a face and color from the identity catalog.")
        agent = self.store.create("agents", {
            "id":agent_id,"face":args.get("face") or face_for(agent_id),
            "name": name, "role": role, "description": str(args.get("description", ""))[:10000],
            "instructions": str(args.get("instructions", ""))[:20000], "category": str(args.get("category") or actor.get('category') or 'custom').strip()[:80],
            "color": args.get("color", COLORS[len(self.store.all("agents")) % len(COLORS)]),
            "avatar": name[0].upper(), "status": "idle", "provider": binding["provider"], "model": binding["model"],
            "binding_origin": binding["binding_origin"], "model_policy": binding["model_policy"],
            "reasoning_effort": binding.get("reasoning_effort"),
            "inherited_model_policy": binding["inherited_model_policy"], "parent_id": args.get("parent_id") or actor["id"],
            "tools": list(dict.fromkeys(tools)), "skills": list(dict.fromkeys(args.get("skills", []))),
            "heartbeat": True, "created_by": actor["id"], "project_id": run.get("project_id"),
        })
        self.store.event('Created ' + name, role, "agent")
        self.agent_event(actor, run, "created", agent)
        return {k: agent[k] for k in ("id", "name", "role", "provider", "model", "binding_origin")}

    EVENT_FIELDS = {"name": "name", "role": "role", "description": "description", "instructions": "instructions", "category": "category",
                    "color": "color", "face": "look", "skills": "skills", "tools": "tools", "heartbeat": "heartbeat", "model": "model"}

    def agent_event(self, actor, run, event, target, fields=()):
        """One compact owner-visible card when an agent creates or updates a teammate (not folded into the work log).

        Several creations from the same run within 60 s share one card."""
        channel = run.get("owner_channel") or run.get("channel") or "dm-" + actor["id"]
        binding = self.engine.models.base(target)
        model = "/".join(p for p in (binding.get("provider"), binding.get("model")) if p)
        item = {"id": target["id"], "name": target["name"], "role": target.get("role", ""), "model": model}
        who = actor["name"]
        if event == "updated":
            labels = list(dict.fromkeys(self.EVENT_FIELDS.get(f, f) for f in fields if f not in ("updated_at", "version")))
            text = who + " updated **" + target["name"] + "**" + (": " + ", ".join(labels) if labels else "")
            return self.emit(actor["id"], text, {**run, "channel": channel}, "agent_event", audience="owner", event="updated",
                             items=[item], subject_ids=[target["id"]], changed=labels)
        line = lambda i: "**" + i["name"] + "** \u00b7 " + (i["role"] or "Agent") + (" \u00b7 " + i["model"] if i["model"] else "")
        previous = next((m for m in reversed(self.store.all("messages")) if m.get("kind") == "agent_event" and m.get("event") == "created"
                         and m.get("run_id") == run["id"] and m.get("channel") == channel), None)
        if previous and self.recent(previous.get("created_at"), 60):
            items = [i for i in previous.get("items", []) if i["id"] != item["id"]] + [item]
            text = who + " created " + str(len(items)) + " agents\n" + "\n".join("- " + line(i) for i in items)
            return self.store.patch("messages", previous["id"], {"content": text, "items": items, "subject_ids": [i["id"] for i in items]})
        return self.emit(actor["id"], who + " created " + line(item), {**run, "channel": channel}, "agent_event", audience="owner",
                         event="created", items=[item], subject_ids=[target["id"]])

    @staticmethod
    def recent(stamp, seconds):
        from datetime import datetime, timezone
        try:
            return (datetime.now(timezone.utc) - datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))).total_seconds() <= seconds
        except ValueError:
            return False

    def update_agent(self, actor, args, run=None):
        if any(key in args for key in ("credentials", "parent_id")):
            raise DomainError("Do not change model configuration, credentials or permission boundaries. These remain owner-managed.")
        target = self.store.get("agents", args.get("agent_id", actor["id"]))
        check_agent_text(args)
        routing = {key: args[key] for key in ("provider", "model", "inherit", "model_policy", "reasoning_effort") if key in args}
        if routing:
            self.engine.models.configure(actor, {"agent_id": target["id"], **routing})
            target = self.store.get("agents", target["id"])
        changes = {key: args[key] for key in ("name", "role", "description", "instructions", "category", "color", "face", "skills", "tools", "heartbeat") if key in args}
        if changes.get("face") and changes["face"] not in FACES or changes.get("color") and changes["color"] not in COLORS:
            raise DomainError("Invalid agent identity.")
        if "role" in changes and changes["role"] != target.get("role"):
            changes = {"skills": [], "instructions": "", "tools": INTERNAL_TOOLS, **changes}
        for skill_id in changes.get("skills", []):
            self.store.get("skills", skill_id)
        if changes.get("tools") is not None and (not isinstance(changes["tools"], list) or set(changes["tools"]) - TOOLS):
            raise DomainError("Some tools are not supported by the runtime.")
        if not changes and not routing:
            raise DomainError("Specify the role, instructions, skills or tools to improve.")
        self.store.create("agent_revisions", {"agent_id": target["id"], "snapshot": target, "changed_by": actor["id"]})
        updated = self.store.patch("agents", target["id"], changes) if changes else target
        self.store.event('Updated ' + updated["name"], updated["role"], "agent")
        fields = list(changes) + (["model"] if routing else [])
        if run:
            self.agent_event(actor, run, "updated", updated, fields)
        return {k: updated.get(k) for k in ("id", "name", "role", "description", "instructions", "tools", "skills", "model_policy", "provider", "model", "version")}

    def send_peer(self, actor, args, run):
        target = self.store.get("agents", args.get("agent_id", ""))
        if target.get("archived") or target["status"] == "paused":
            raise DomainError("This teammate is paused or archived.")
        content = str(args.get("content", "")).strip()[:18000]
        if not content or target["id"] == actor["id"]:
            raise DomainError("Choose another teammate and provide message content.")
        require_english("Internal team messages", content)
        fingerprint = hashlib.sha256((actor["id"] + target["id"] + re.sub(r"\s+", " ", content.casefold())).encode()).hexdigest()
        duplicates = [m for m in self.store.all("messages") if m.get("fingerprint") == fingerprint and m.get("project_id") == run.get("project_id")]
        if len(duplicates) >= 2:
            return {"delivered": False, "reason": 'This message was already sent twice. Read the reply and adjust the request rather than repeating it.'}
        incoming = self.emit(actor["id"], content, {**run, "channel": "dm-" + target["id"]}, "peer",
                             target_id=target["id"], fingerprint=fingerprint, source_channel=run["channel"])
        event = self.emit(actor["id"], 'Messaged ' + target["name"], run, "handoff",
                          target_id=target["id"], related_message_id=incoming["id"])
        if not args.get("request_reply", True):
            return {"delivered": True, "message_id": incoming["id"], "event_id": event["id"]}
        peer = self.engine.enqueue(target["id"], content, "peer", project_id=run.get("project_id"),
                    root_id=run.get("root_id", run["id"]), channel="dm-" + target["id"],
                    reply_to_agent_id=actor["id"], reply_to_run_id=run["id"], reply_to_channel=run["channel"],
                    handoff_event_id=event["id"], source_message_id=incoming["id"])
        self.store.patch("runs", peer["id"], {"owner_outbound": run.get("owner_outbound") or run.get("outbound"),
                                             "owner_channel": run.get("owner_channel", run["channel"])})
        if args.get("wait"):
            current = self.store.get("runs", run["id"])
            self.store.patch("runs", run["id"], {"wait_for_runs": list(dict.fromkeys(current.get("wait_for_runs", []) + [peer["id"]]))})
        return {"delivered": True, "run_id": peer["id"], "target_id": target["id"], "event_id": event["id"]}

    def delegate(self, actor, args, run):
        target = self.store.get("agents", args.get("agent_id", ""))
        title = str(args.get("title", "")).strip()
        if not title:
            raise DomainError("A task title is required.")
        require_english("Task title and brief", title, args.get("description", ""))
        reviewer = args.get("reviewer_id")
        if not reviewer and args.get("auto_review", True):
            reviewer = actor["id"] if target["id"] != actor["id"] else next(
                (a["id"] for a in self.store.all("agents") if a["id"] != target["id"] and a["status"] != "paused" and not a.get("archived")), None)
        if reviewer:
            self.store.get("agents", reviewer)
            if reviewer == target["id"]:
                raise DomainError("The reviewer must differ from the task owner.")
        deps = args.get("dependencies", [])
        for dep in deps:
            self.store.get("tasks", dep)
        existing = next((t for t in self.store.all("tasks") if t.get("project_id") == run.get("project_id")
                         and t.get("title") == title and t.get("agent_id") == target["id"] and t["status"] not in TERMINAL), None)
        if existing:
            return {"task_id": existing["id"], "status": existing["status"], "existing": True}
        selection = self.engine.models.selection(target, args, {})
        task = self.store.create("tasks", {
            "title": title[:250], "description": str(args.get("description", ""))[:30000],
            "agent_id": target["id"], "reviewer_id": reviewer, "coordinator_id": actor["id"],
            "status": "ready", "auto_run": True, "auto_review": bool(reviewer),
            "dependencies": deps, "priority": args.get("priority", "medium"), "category": target.get("category", "custom"),
            "project_id": run.get("project_id"), "parent_run_id": run["id"], "root_id": run.get("root_id", run["id"]),
            "return_channel": run["channel"], "tags": [], "due": args.get("due", ""),
            "owner_outbound": run.get("owner_outbound") or run.get("outbound"),
            "model_selection": selection,
        })
        self.emit(actor["id"], 'Assigned work to ' + target["name"], run, "assignment", target_id=target["id"], task_id=task["id"])
        return {"task_id": task["id"], "agent_id": target["id"], "reviewer_id": reviewer, "status": "ready"}

    def task_review(self, actor, args, run):
        task = self.store.get("tasks", args.get("task_id", ""))
        if task.get("reviewer_id") != actor["id"] or task["agent_id"] == actor["id"] or task["status"] != "review":
            raise DomainError("Only the assigned reviewer can evaluate this pending draft.", 403)
        decision = args.get("decision")
        if decision not in ("approve", "revise"):
            raise DomainError("Reviews require decision=approve or revise.")
        score = int(args.get("score", 4))
        if not 1 <= score <= 5:
            raise DomainError("Scores must be between 1 and 5.")
        note = str(args.get("note", ""))[:20000]
        if not note.strip():
            raise DomainError("Provide specific review comments.")
        revision = task.get("revision", 0) + (decision == "revise")
        self.store.patch("tasks", task["id"], {"status": "done" if decision == "approve" else "ready",
            "score": score, "review_note": note, "review_feedback": note, "reviewed_at": now(), "revision": revision,
            "review_run_id": None, "last_review_run_id": run["id"]})
        self.store.create("memories", {"title": 'Feedback: ' + task["title"][:180],
            "content": f"{score}/5 · {decision}\n{note}", "scope": "private", "agent_id": task["agent_id"],
            "type": "feedback", "tags": ['Reconcile'], "project_id": task.get("project_id")})
        self.store.event('Reviewed ' + task["title"][:120], actor["name"] + ": " + decision, "task")
        if revision >= 3 and decision == "revise":
            self.ask(actor, {"question": 'This work needs several revision rounds. Which criteria would you like to adjust?',
                "context": task["title"] + "\n" + note, "blocking": False}, run)
        return {"task_id": task["id"], "status": "done" if decision == "approve" else "ready", "score": score}

    def submit_task(self, actor, args):
        task = self.store.get("tasks", args.get("task_id", ""))
        if task["agent_id"] != actor["id"] or task["status"] != "running":
            raise DomainError("Only the task owner can submit a draft for a running task.", 409)
        result = str(args.get("result", "")).strip()
        if not result:
            raise DomainError("Provide results and deliverable paths for review.")
        updated = self.store.patch("tasks", task["id"], {"status": "review" if task.get("reviewer_id") else "done",
                                                        "result": result[:30000]})
        if updated["status"] == "review":
            self.engine.review(task["id"])
        return {"task_id": task["id"], "status": updated["status"], "submitted": True}

    def ask(self, actor, args, run, _routed=False):
        if args.get("kind") == "folder_access" and not _routed:
            return self.engine.access.request(actor, {"path": args.get("requested_path"),
                "access": args.get("access", "read"), "target_type": args.get("target_type"),
                "reason": args.get("context") or args.get("question")}, run)
        question = str(args.get("question", "")).strip()
        if not question:
            raise DomainError("Provide a clear question.")
        current = next((q for q in self.store.all("decisions") if q.get("run_id") == run["id"]
                        and q.get("question") == question and q["status"] == "open"), None)
        if current:
            return {"decision_id": current["id"], "status": "open"}
        kind = args.get("kind", "choice" if args.get("options") else "input")
        if kind not in ("choice", "input", "approval", "folder_access", "model_config"):
            raise DomainError("This decision type is not supported.")
        options = []
        for index, option in enumerate(args.get("options", [])[:8]):
            options.append({"value": str(option.get("value", index)) if isinstance(option, dict) else str(option),
                            "label": str(option.get("label", option.get("value", index))) if isinstance(option, dict) else str(option)})
        decision = self.store.create("decisions", {"question": question[:2000], "context": str(args.get("context", ""))[:10000],
            "options": options, "agent_id": actor["id"], "run_id": run["id"], "project_id": run.get("project_id"),
            "channel": run.get("owner_channel") or run.get("channel") or "dm-" + actor["id"], "root_id": run.get("root_id", run["id"]),
            "kind": kind, "status": "open", "blocking": bool(args.get("blocking", True)),
            "requested_path": str(args.get("requested_path", ""))[:1000],
            **self._decision_actions(args, _routed)})
        if decision["blocking"]:
            active = self.store.get("runs", run["id"])
            self.store.patch("runs", run["id"], {"wait_for_decisions": list(dict.fromkeys(active.get("wait_for_decisions", []) + [decision["id"]]))})
        self.emit(actor["id"], question, {**run, "channel": decision["channel"]}, "decision", decision_id=decision["id"])
        outbound = run.get("owner_outbound") or run.get("outbound")
        if outbound and outbound.get("channel") == "telegram":
            text = question + "\n" + "\n".join(str(i) + ". " + o["label"] for i, o in enumerate(decision.get("actions") or options, 1))
            text += '\nReply: /answer ' + decision["id"] + ' <option or answer>'
            self.engine.queue_reply(outbound, text, "decision:" + decision["id"])
        return {"decision_id": decision["id"], "status": "open", "blocking": decision["blocking"]}

    @staticmethod
    def _decision_actions(args, routed):
        """Runtime-owned action buttons. Agents cannot attach privileged handlers."""
        if not routed or not args.get("actions"):
            return {}
        actions = [{"value": str(a["value"])[:60], "label": str(a.get("label") or a["value"])[:160],
                    "style": a.get("style") if a.get("style") in ("primary", "quiet", "danger") else "quiet",
                    **({"hint": str(a["hint"])[:240]} if a.get("hint") else {})}
                   for a in args["actions"][:6] if isinstance(a, dict) and a.get("value")]
        field, handler = str(args.get("action_field", "action")), str(args.get("action_handler", ""))
        return {"actions": actions, "action_field": field, "action_handler": handler} if actions else {}

    def answer(self, decision_id, answer):
        with self.store.transaction():
            question = self.store.get("decisions", decision_id)
            if question["status"] != "open":
                raise DomainError("This question has already been answered.", 409)
            text = str(answer.get("answer", "")).strip()
            if not text:
                raise DomainError("Select an option or enter an answer.")
            if len(text) > 20000:
                raise DomainError("The answer is too long.")
            handler = self.engine.decision_handlers.get(question.get("action_handler") or "")
            if question.get("actions") and handler:
                field = question.get("action_field") or "action"
                if field not in answer:
                    # Telegram `/answer <id> <text>` and typed replies: match a button by label, value or 1-based number.
                    typed = text.casefold()
                    for index, action in enumerate(question["actions"], 1):
                        if typed in (action["label"].casefold(), action["value"].casefold(), str(index)):
                            answer = {**answer, field: action["value"]}
                            break
                value = answer.get(field)
                if value not in {a["value"] for a in question["actions"]}:
                    raise DomainError("Choose one of the decision buttons.", 400)
                handler(question, answer)
            elif question.get('safety_request'):
                self.engine.safety.answer(question, answer)
            elif question.get('command_request'):
                self.engine.commands.approval(question, answer)
            elif question.get("permission_request"):
                self.engine.access.approval(question, answer)
            elif question.get('browser_origin'):
                self.engine.browser.approval(question,answer)
            elif question["kind"] == "folder_access" and answer.get("grant"):
                self.grant_folder(question.get("project_id"), answer["grant"])
            updated = self.store.patch("decisions", decision_id, {"status": "answered", "answer": text, "answered_at": now()})
            run = self.store.get("runs", question["run_id"])
            self.store.create("messages", {"role": "user", "agent_id": question["agent_id"], "kind": "decision_answer",
                "content": text, "channel": question["channel"], "project_id": question.get("project_id"), "decision_id": decision_id})
            self.engine.wake_waiters(check_accounts=False)
            if run["status"] in TERMINAL and not question["blocking"] and not question.get("action_handler"):
                self.engine.enqueue(question["agent_id"], 'Owner reply: ' + text, "decision_followup",
                    channel=question["channel"], project_id=question.get("project_id"), root_id=question["root_id"],
                    owner_outbound=run.get("owner_outbound"), outbound=run.get("outbound"))
            return updated

    def grant_folder(self, project_id, data):
        project = self.store.get("projects", project_id)
        target_type = data.get("target_type", "folder")
        access = data.get("access", "read")
        if access not in ("read", "readwrite"):
            raise DomainError("Folder access must be read or readwrite.")
        create = data.get('create_directory') is True and target_type == 'folder' and access == 'readwrite'
        path = self.engine.files.validate_grant(Path(str(data.get("path", ""))).expanduser(), target_type, allow_create=create)
        if create:
            path.mkdir(parents=True, exist_ok=True)
            self.engine.files.validate_grant(path, target_type)
        grants = [g for g in project.get("folder_grants", []) if not (
            g["path"] == str(path.parent if target_type == "file" else path)
            and g.get("file") == (path.name if target_type == "file" else None))]
        grants.append({"id": uid("folder"), "path": str(path.parent if target_type == "file" else path),
            "target_type": target_type, "file": path.name if target_type == "file" else None,
            "access": access, "granted_by": "owner", "granted_at": now()})
        return self.store.patch("projects", project_id, {"folder_grants": grants})

    def invoke(self, name, args, actor, run):
        if name == "files.request_access":
            return self.engine.access.request(actor, args, run)
        if name in ("chat.send", "progress.update"):
            text = str(args.get("text") or args.get("content") or "")
            if name == "progress.update":
                self.store.patch("agents", actor["id"], {"current_action": text[:180]})
                if args.get("item_id"):
                    return self.engine.progress.set({"items":[{"id":args["item_id"],**{k:v for k,v in args.items() if k in ("status","note","title","agent_ids")}}]},actor,run)
                return {"updated": True}
            attached = {"artifact_ids": [], "notes": []}
            if name == "chat.send" and args.get("files") and run.get("kind") != "support":
                attached = self.engine.outputs.attach(args["files"], actor, run)
            extra = {"artifact_ids": attached["artifact_ids"]} if attached["artifact_ids"] else {}
            note = {**({"artifact_ids": attached["artifact_ids"]} if attached["artifact_ids"] else {}), **({"attachment_errors": attached["notes"]} if attached["notes"] else {})}
            if args.get("audience")=="owner":
                return {"message_id":self.emit(actor["id"],text,{**run,"channel":run.get("owner_channel") or "dm-"+actor["id"]},audience="owner",**extra)["id"],**note}
            return {"message_id": self.emit(actor["id"], text, run, **extra)["id"], **note}
        if name == "agents.create":
            return self.create_agent(actor, args, run)
        if name == "agents.update":
            return self.update_agent(actor, args, run)
        if name == "agents.archive":
            target = self.store.get("agents", args.get("agent_id", ""))
            if target["id"] in ("a-lead", actor["id"]):
                raise DomainError("Keep Atlas and the current actor so coordination can continue.")
            pending = [t for t in self.store.all("tasks") if target["id"] in (t.get("agent_id"), t.get("reviewer_id")) and t["status"] not in ("done", "cancelled") and not t.get("sample")]
            replacement = self.store.get("agents", args["reassign_to"]) if args.get("reassign_to") else None
            if pending and (not replacement or replacement["id"]==target["id"] or replacement.get("archived")):
                raise DomainError("This teammate has unfinished work. Choose reassign_to before archiving.")
            for task in pending:
                performer = replacement["id"] if task["agent_id"]==target["id"] else task["agent_id"]
                reviewer = replacement["id"] if task.get("reviewer_id")==target["id"] else task.get("reviewer_id")
                if performer == reviewer:
                    raise DomainError("The new owner cannot review their own work.")
            for active in self.store.all("runs"):
                if active["agent_id"]==target["id"] and active["status"] not in TERMINAL:
                    self.engine.cancel(active["id"])
            for task in pending:
                self.store.patch("tasks", task["id"], {"agent_id": replacement["id"] if task["agent_id"]==target["id"] else task["agent_id"],
                    "reviewer_id": replacement["id"] if task.get("reviewer_id")==target["id"] else task.get("reviewer_id"),
                    "status": "ready" if task["agent_id"]==target["id"] else task["status"]})
            for routine in self.store.all("schedules"):
                if routine.get("agent_id")==target["id"]:
                    self.store.patch("schedules",routine["id"],{"enabled":False})
            self.store.create("agent_revisions", {"agent_id":target["id"], "snapshot":target, "changed_by":actor["id"]})
            self.store.patch("agents",target["id"],{"archived":True,"heartbeat":False,"status":"idle"})
            return {"archived":True,"agent_id":target["id"],"transferred_tasks":[t["id"] for t in pending]}
        if name == "agents.inspect":
            if not args.get("agent_id"):  # roster: no instructions/tools/policy; ask by agent_id for the full record
                return [{**{k: a.get(k) for k in ("id", "name", "role", "category", "parent_id", "status", "provider", "model")},
                         "skills": len(a.get("skills") or [])} for a in self.store.all("agents") if not a.get("archived")]
            result = []
            for a in [self.store.get("agents", args["agent_id"])]:
                if a.get("archived"):
                    continue
                record = {k: a.get(k) for k in ("id", "name", "face", "color", "role", "category", "parent_id", "description", "instructions", "tools", "skills", "status", "provider", "model", "binding_origin", "model_policy", "reasoning_effort")}
                text = str(record.get("instructions") or "")
                if len(text) > 3000:
                    record["instructions"] = text[:3000] + "…[truncated %d chars]" % (len(text) - 3000)
                result.append(record)
            return result
        if name == "tools.inspect":
            return inspect_tools(args, self.engine.custom_tools.list())
        if name == "tools.enable":
            target = self.store.get("agents", args.get("agent_id", actor["id"]))
            names = args.get("tools", [])
            if not isinstance(names, list) or set(names) - TOOLS:
                raise DomainError("Only enable tools available in this runtime.")
            self.store.patch("agents", target["id"], {"tools": sorted(set(target.get("tools", [])) | set(names))})
            return {"agent_id": target["id"], "enabled": names}
        if name == "team.message":
            return self.send_peer(actor, args, run)
        if name == "team.wait":
            if run['id'] in args.get('run_ids',[]):
                raise DomainError('Cannot wait for your own run. Finish the step or ask a teammate for a specific result.',409)
            for task_id in args.get('task_ids',[]):
                task=self.store.get('tasks',task_id)
                if run['id'] in (task.get('run_id'),task.get('review_run_id')):
                    raise DomainError('Cannot wait for the task you are executing/reviewing. Submit or review it first.',409)
                if run.get('review_task_id')==task_id or run['id'] in (task.get('review_run_id'),task.get('last_review_run_id')):
                    raise DomainError('You are the reviewer: the runtime wakes you when the draft is resubmitted; finish this turn.',409)
            for key, kind in (("run_ids", "runs"), ("task_ids", "tasks")):
                for item in args.get(key, []):
                    doc = self.store.get(kind, item)
                    if doc.get("project_id") != run.get("project_id"):
                        raise DomainError("Only wait for work in the same project.")
            self.store.patch("runs", run["id"], {"wait_for_runs": list(dict.fromkeys(args.get("run_ids", []))),
                                               "wait_for_tasks": list(dict.fromkeys(args.get("task_ids", []))),
                                               "wait_reason": str(args.get("reason", ""))[:1000]})
            return {"waiting": True}
        if name == "tasks.delegate":
            return self.delegate(actor, args, run)
        if name == "tasks.submit":
            return self.submit_task(actor, args)
        if name == "tasks.inspect":
            if args.get("task_id"):
                task = dict(self.store.get("tasks", args["task_id"]))
                if len(str(task.get("result") or "")) > 8000:
                    task["result"] = str(task["result"])[:8000] + "…[truncated %d chars]" % (len(str(task["result"])) - 8000)
                return task
            return [{**{k: t.get(k) for k in ("id", "title", "status", "agent_id", "reviewer_id")},
                     "result": (str(t["result"])[:300] + ("…" if len(str(t["result"])) > 300 else "")) if t.get("result") else None}
                    for t in self.store.all("tasks") if t.get("project_id") == run.get("project_id") and not t.get("archived")]
        if name == "tasks.update":
            task = self.store.get("tasks", args.get("task_id", ""))
            changes = {k: args[k] for k in ("title", "description", "agent_id", "status") if k in args}
            if changes.get("status") not in (None, "ready", "cancelled"):
                raise DomainError("Complete work through deliverable submission and review.")
            if task["status"] == "running":
                raise DomainError("This work is running. Send more context to its owner or ask the project owner to stop it.")
            if changes.get("agent_id"):
                self.store.get("agents", changes["agent_id"])
            return self.store.patch("tasks", task["id"], changes)
        if name == "tasks.review":
            return self.task_review(actor, args, run)
        if name == "decisions.ask":
            return self.ask(actor, args, run)
        if name == "decisions.record_answer":
            question = self.store.get("decisions", args.get("decision_id", ""))
            messages = [m for m in self.store.all("messages") if m.get("run_id") == run["id"]
                        and m["role"] == "user" and m.get("channel") == question.get("channel")
                        and self.store.position("messages", m["id"]) > self.store.position("decisions", question["id"])]
            if (run.get("kind") != "chat" or not messages or question["kind"] == "model_config" or question.get('safety_request') or question.get('command_request')
                    or question["run_id"] == run["id"]):
                raise DomainError("Only record an actual owner reply received after the question. Never fabricate an answer.", 403)
            return self.answer(question["id"], {"answer": messages[-1]["content"]})
        if name == "skills.search":
            return self.engine.skills.search(args.get("query", ""))
        if name == "skills.research":
            return self.engine.skills.research(args.get("query", ""), args.get("url"))
        if name == "skills.scan":
            return self.engine.skills.scan()
        if name == "skills.install":
            return self.engine.skills.install(args, args.get("agent_id", actor["id"]))
        if name in ("skills.create", "skills.update"):
            return self.engine.skills.save(args, actor["id"], args.get("skill_id") if name.endswith("update") else None)
        if name == "memory.search":
            query = str(args.get("query", "")).casefold()
            memories = [m for m in self.store.all("memories") if not m.get("archived") and (
                m.get("scope") == "shared" or (run.get("kind") != "support" and m.get("agent_id") == actor["id"]))]
            words = query.split()
            memories.sort(key=lambda m: sum(w in (m["title"] + " " + m["content"]).casefold() for w in words), reverse=True)
            return [{k: m.get(k) for k in ("id", "title", "content", "scope")} for m in memories[:12]]
        if name == "memory.write":
            scope = args.get("scope", "private")
            if scope not in ("shared", "private"):
                raise DomainError("Memory scope must be private or shared.")
            require_english("Memory", args.get("title", ""), args.get("content", ""))
            return self.store.create("memories", {"title": str(args.get("title", 'Memory'))[:250],
                "content": self.engine.vault.redact(str(args.get("content", ""))[:30000]), "scope": scope,
                "agent_id": actor["id"] if scope == "private" else None, "type": "memory", "tags": [],
                "project_id": run.get("project_id")})
        if name in ("routines.create", "routines.update"):
            from .schedule import next_schedule
            if name == "routines.create":
                doc = {"name": str(args.get("name", "")), "prompt": str(args.get("prompt", "")),
                    "agent_id": args.get("agent_id", actor["id"]), "mode": args.get("mode", "daily"),
                    "time": args.get("time", "08:30"), "interval_minutes": int(args.get("interval_minutes", 60)),
                    "enabled": True, "created_by": actor["id"]}
                self.store.get("agents", doc["agent_id"])
                if not doc["name"] or not doc["prompt"]:
                    raise DomainError("Schedules require a name and work instructions.")
                doc["next_run"] = next_schedule(doc)
                return self.store.create("schedules", doc)
            routine = self.store.get("schedules", args.get("routine_id", ""))
            changes = {k: args[k] for k in ("name", "prompt", "mode", "time", "interval_minutes", "enabled") if k in args}
            changes["next_run"] = next_schedule({**routine, **changes})
            return self.store.patch("schedules", routine["id"], changes)
        if name == "projects.create":
            project = self.store.create("projects", {"title": str(args.get("title", 'New project'))[:250],
                "brief": str(args.get("brief", ""))[:30000], "instructions": str(args.get("instructions", ""))[:10000],
                "goals": Goals.initial(str(args.get("title", 'New project'))[:250], str(args.get("brief", ""))[:30000], actor["id"]),
                "owner_agent_id": actor["id"], "channel": run["channel"], "status": "active", "folder_grants": []})
            self.store.patch("runs", run["id"], {"project_id": project["id"]})
            return project
        if name == "projects.update":
            project = self.store.get("projects", args.get("project_id") or run.get("project_id"))
            changes = {k: args[k] for k in ("title", "brief", "instructions", "status") if k in args}
            if changes.get("status") == "done":
                pending = [t for t in self.store.all("tasks") if t.get("project_id") == project["id"] and t["status"] not in TERMINAL]
                if pending or any(q["status"] == "open" and q.get("project_id") == project["id"] for q in self.store.all("decisions")):
                    raise DomainError("Work or questions remain unresolved. Summarize blockers instead of marking complete.")
            return self.store.patch("projects", project["id"], changes)
        raise DomainError("Unsupported tool: " + name)
