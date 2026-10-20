"""Project file grants, requested through Atlas and approved by the owner."""
import re
from pathlib import Path

from .store import DomainError, now


def mentioned_paths(text):
    """Extract absolute local paths, including quoted paths with spaces."""
    quoted = re.findall(r"""["'\x60“]([A-Za-z]:[\\/][^"'\x60”\r\n]+|/[^"'\x60”\r\n]+)["'\x60”]""", str(text))
    bare = re.findall(r"""(?<!\w)[A-Za-z]:[\\/][^\s"'\x60<>|,;!?，。]*""", str(text))
    values = []
    for value in quoted + bare:
        value = value.rstrip(" .,:;)]}”'\"\x60")
        if value and value not in values:
            values.append(value)
    return values


def permission_blocked(text):
    text = str(text).casefold()
    return any(token in text for token in (
        "ngoài workspace", "ngoài thư mục", "chưa thể đọc", "không thể đọc",
        "không có quyền đọc", "chưa có quyền", "chưa được cấp", "chưa kiểm tra được",
        "outside the workspace", "outside workspace", "cannot access", "access denied"))


class AccessManager:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def normalized(self, value):
        value = str(value).strip().strip('"\x60')
        path = Path(value).expanduser()
        if not path.is_absolute() or value.startswith(("\\\\", "//")) or ".." in path.parts:
            raise DomainError("Use an absolute path on the runtime machine without network paths or parent traversal.")
        return path.resolve()

    def covered(self, project, path, access="read"):
        for grant in project.get("folder_grants", []):
            root = Path(grant["path"]).resolve()
            if access == "readwrite" and grant.get("access") != "readwrite":
                continue
            if grant.get("target_type") == "file":
                if path == root / grant["file"]:
                    return grant
            elif path.is_relative_to(root):
                return grant
        return None

    def source_paths(self, run):
        candidates = mentioned_paths(run.get("prompt", ""))
        messages = [m for m in self.store.all("messages") if m.get("role") == "user"
                    and m.get("project_id") == run.get("project_id") and not m.get("sample")]
        for message in reversed(messages[-15:]):
            candidates += mentioned_paths(message.get("content", ""))
        return list(dict.fromkeys(candidates))

    def request(self, actor, args, run):
        if run.get("kind") == "support":
            raise DomainError("Customer messages cannot request file access.", 403)
        project_id = run.get("project_id") or args.get("project_id")
        if not project_id:
            raise DomainError("Link this work to a project before requesting access.")
        if run.get("project_id") and args.get("project_id") not in (None, project_id):
            raise DomainError("Request access only for the current project.", 403)
        project = self.store.get("projects", project_id)
        path = self.normalized(args.get("path") or args.get("requested_path") or "")
        access = args.get("access", "read")
        if access not in ("read", "readwrite"):
            raise DomainError("Permission must be read or readwrite.")
        target_type = args.get("target_type") or ("file" if path.is_file() else "folder")
        if target_type not in ("file", "folder"):
            raise DomainError("Scope must be file or folder.")
        create = target_type == 'folder' and access == 'readwrite' and not path.exists()
        self.engine.files.validate_grant(path, target_type, allow_create=create)
        if path.is_relative_to(self.engine.workspace) and access == "read":
            return {"status": "granted", "scope": "company", "path": str(path)}
        grant = self.covered(project, path, access)
        if grant:
            return {"status": "granted", "grant_id": grant["id"], "project_id": project_id,
                    "path": str(path), "access": grant["access"]}
        key = str(path).casefold()
        with self.store.transaction():
            records = [q for q in self.store.all("decisions") if q.get("permission_request")
                       and q.get("project_id") == project_id and q.get("path_key") == key
                       and q.get("requested_access") == access and q.get("target_type") == target_type]
            current = next((q for q in reversed(records) if q["status"] == "open"), None)
            declined = next((q for q in reversed(records) if q.get("permission_status") in ("denied", "partial")), None)
            if not current and declined:
                return {"status": "denied", "access_denied": True, "decision_id": declined["id"],
                        "error": 'The owner has not granted this access. Continue authorized work without repeating the same request.'}
            if current:
                decision = current
            else:
                coordinator = self.store.get("agents", "a-lead")
                channel = "dm-a-lead"
                outbound = run.get("owner_outbound") or run.get("outbound")
                if outbound and outbound.get("channel") == "telegram":
                    channel = "telegram:" + outbound["recipient"]
                approval = self.engine.enqueue("a-lead", 'Process the project access request through Atlas.',
                    "access_approval", project_id=project_id, root_id=run.get("root_id", run["id"]),
                    channel=channel, owner_channel=channel, owner_outbound=outbound,
                    status="waiting_user", system_managed=True)
                level = 'read and edit' if access == "readwrite" else 'read'
                reason = str(args.get("reason") or 'Read the actual source to continue the assigned task.')[:3000]
                asked = self.engine.team.ask(coordinator, {
                    "question": 'Allow the team to ' + level + " " + ('file ' if target_type == "file" else 'folder ') + str(path) + ' for this project?',
                    "context": "@{"+actor["id"]+"} cần quyền để tiếp tục.\n" + reason
                        + '\nAccess is shared within this project. Individual files do not require separate approval.',
                    "kind": "folder_access", "requested_path": str(path),
                    "options": ['Allow ' + level, 'Deny access'], "blocking": True,
                    }, approval, _routed=True)
                decision = self.store.patch("decisions", asked["decision_id"], {
                    "permission_request": True, "permission_status": "pending", "path_key": key,
                    "create_directory": create,
                    "requested_access": access, "target_type": target_type, "requesters": []})
                self.store.patch("runs", approval["id"], {"access_decision_id": decision["id"]})
            requesters = decision.get("requesters", [])
            if not any(r["run_id"] == run["id"] for r in requesters):
                requesters.append({"run_id": run["id"], "agent_id": actor["id"],
                                   "reason": str(args.get("reason", ""))[:3000]})
                self.store.patch("decisions", decision["id"], {"requesters": requesters})
                if actor["id"] != "a-lead":
                    self.engine.team.emit(actor["id"], 'Requesting access through Atlas: ' + access + ": " + str(path),
                        {**run, "channel": "dm-a-lead"}, "peer", target_id="a-lead", decision_id=decision["id"])
                    self.engine.team.emit(actor["id"], 'File access request forwarded to Atlas.', run,
                                          "handoff", target_id="a-lead", decision_id=decision["id"])
            active = self.store.get("runs", run["id"])
            self.store.patch("runs", run["id"], {"wait_for_decisions": list(dict.fromkeys(
                active.get("wait_for_decisions", []) + [decision["id"]]))})
            return {"status": "waiting_owner", "waiting_permission": True, "decision_id": decision["id"],
                    "coordinator_id": "a-lead", "project_id": project_id, "path": str(path), "access": access}

    def prepare(self, actor, name, args, run):
        """Normalize absolute broker paths and request missing grants automatically."""
        args = dict(args)
        is_project = name.startswith("project.")
        write = name.endswith((".write", ".mkdir"))
        access = "readwrite" if write else "read"
        raw = args.get("path")
        absolute = bool(raw and Path(str(raw)).is_absolute())
        project_id = run.get("project_id") or args.get("project_id")
        if absolute:
            path = self.normalized(raw)
            if path.is_relative_to(self.engine.workspace):
                args["path"] = str(path.relative_to(self.engine.workspace))
                return args, False, None
            if not project_id:
                raise DomainError("A project is required for Atlas to request access to this path.")
            project = self.store.get("projects", project_id)
            grant = self.covered(project, path, access)
            if not grant:
                result = self.request(actor, {"path": str(path), "access": access,
                    "target_type": "folder" if name.endswith('.mkdir') else "file" if write or path.is_file() else "folder",
                    "reason": 'Tool ' + name + ' needs access outside the company workspace.'}, run)
                return args, True, result
            args.update(project_id=project_id, grant_id=grant["id"], path=str(path.relative_to(Path(grant["path"]))))
            return args, True, None
        if not is_project:
            return args, False, None
        if project_id:
            args["project_id"] = project_id
            project = self.store.get("projects", project_id)
            grants = project.get("folder_grants", [])
            selected = next((g for g in grants if g["id"] == args.get("grant_id")), None)
            if args.get("grant_id") and not selected:
                raise DomainError("This permission does not belong to the current project.", 403)
            if not selected and not args.get("grant_id") and grants:
                selected = grants[0]
            if selected and (not write or selected.get("access") == "readwrite"):
                return args, True, None
            paths = [str(Path(selected["path"]) / selected["file"]) if selected and selected.get("target_type") == "file"
                     else selected["path"]] if selected else self.source_paths(run)
            if paths:
                return args, True, self.request(actor, {"path": paths[0], "access": access,
                    "target_type": selected.get("target_type", "folder") if selected else None,
                    "reason": 'Access is required to continue ' + name + "."}, run)
        raise DomainError("Source folder unknown. Use files.request_access with the requested path so Atlas can ask the owner.")

    def approval(self, question, answer):
        """Only owner endpoints/verified newer owner messages call this method."""
        text = str(answer.get("answer", "")).strip()
        folded = text.casefold()
        deny = answer.get("approve") is False or bool(re.search(
            r"\b(?:không|đừng|chớ)(?:\s+(?:cần|muốn|nên|vội))*\s+(?:cấp|cho phép|đồng ý)|\btừ chối\b|\b(?:deny|denied)\b|^(?:không|no|nope)(?:[\s,.!]|$)|\b(?:do not|don't|not)\s+(?:allow|approve|grant)\b", folded))
        explicit = answer.get("grant")
        tentative = bool(re.search(r"\b(?:chưa|cân nhắc|xem xét|có nên|liệu|nếu|sau khi|để tôi)\b|[?？]", folded))
        affirmative = bool(re.match(
            r"^(?:(?:tôi|anh|mình|em|đã)\s+)?(?:cho phép|cấp quyền|đồng ý|chấp thuận|approve|allow|ok(?:ay)?|yes|có|được)(?:\b|$)", folded))
        allow = not deny and not tentative and (answer.get("approve") is True or affirmative)
        if not allow and not deny:
            raise DomainError("Choose Allow or Deny. Access has not changed.")
        grant = None
        if allow:
            data = dict(explicit or {"path": question["requested_path"], "access": question["requested_access"]})
            if "chỉ đọc" in folded:
                data["access"] = "read"
            data["target_type"] = question.get("target_type", "folder")
            data['create_directory'] = bool(question.get('create_directory'))
            granted_path = self.normalized(data["path"])
            if str(granted_path).casefold() != question["path_key"]:
                raise DomainError("The path differs from the Atlas request. Grant the displayed scope or request a corrected path.")
            project = self.engine.team.grant_folder(question["project_id"], data)
            grant = self.covered(project, granted_path, data.get("access", "read"))
        self.store.patch("decisions", question["id"], {
            "permission_status": "granted" if grant and (grant["access"] == "readwrite" or question["requested_access"] == "read") else
                                 "partial" if grant else "denied",
            "granted_access": grant.get("access") if grant else None, "resolved_at": now()})
        self.store.patch("runs", question["run_id"], {"status": "done", "completed_at": now(),
            "result": 'The project owner granted access.' if grant else 'The project owner denied access.'})
        for requester in question.get("requesters", []):
            run = self.store.get("runs", requester["run_id"])
            if run["status"] == "cancelled":
                continue
            self.engine.team.emit("a-lead", ('Access received: ' + grant["access"] + '; resume work.' if grant else
                'The owner denied access. Continue work within existing permissions.'),
                {**run, "channel": "dm-" + requester["agent_id"]}, "access_reply", decision_id=question["id"])
            self.store.patch("runs", run["id"], {"resume_reason": 'Atlas received the access decision: ' + text})
        self.engine.team.emit("a-lead", ('Access granted: ' + grant["access"] + ' for the project. The team has been notified to resume.' if grant else
            'Access denial recorded. The team will continue authorized work.'),
            self.store.get("runs", question["run_id"]), "access_reply", decision_id=question["id"])
        return grant

    def escalate_blocked(self, actor, run, reply):
        if run.get("kind") == "support" or not permission_blocked(reply):
            return None
        paths = mentioned_paths(reply) + self.source_paths(run)
        for raw in dict.fromkeys(paths):
            try:
                path = self.normalized(raw)
                if path.is_relative_to(self.engine.workspace):
                    continue
                return self.request(actor, {"path": str(path), "access": "read",
                    "reason": 'Source documents are missing. Atlas needs to request access so the team can read them directly.'}, run)
            except DomainError:
                continue
        return None

    def recover_blocked(self):
        """Recover the latest prematurely completed source-access work per actor/project."""
        if not self.store.settings().get('execution_enabled',True):return
        seen = set()
        for run in reversed(self.store.all("runs")[-100:]):
            key = (run.get("project_id"), run["agent_id"])
            if key in seen or run.get("kind") == "access_approval" or run.get("archived"):
                continue
            seen.add(key)
            if run["status"] != "done" or not run.get("project_id") or run.get("access_reopened"):
                continue
            if self.engine.safety.halted(run):continue
            reply = run.get("result", run.get("last_reply", ""))
            actor = self.store.get("agents", run["agent_id"])
            result = self.escalate_blocked(actor, run, reply)
            if result and result.get("waiting_permission"):
                self.store.patch("runs", run["id"], {"status": "waiting_user", "access_reopened": True,
                    "phase": "model", "pending_calls": [], "completed_at": None, "result": "",
                    "resume_reason": 'Resuming work that needs access. Waiting for the owner decision requested by Atlas.'})
