import hashlib
import re
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .engine import TOOLS, next_schedule, parse_time
from .store import DomainError, now, uid
from .identity import FACES, COLORS, face_for
from .language import english_authoring
from .skill_catalog import normalize_sources

FIELDS = {
    "agents": {"name", "role", "description", "instructions", "category", "color", "avatar", "face", "provider", "model", "model_policy", "status", "tools", "skills", "heartbeat", "archived"},
    "checklist": {"title","note","status","agent_ids","project_id","task_id","position","archived"},
    "tasks": {"title", "description", "agent_id", "reviewer_id", "priority", "category", "due", "status", "dependencies", "auto_run", "tags", "archived"},
    "memories": {"title", "content", "scope", "agent_id", "type", "tags", "archived"},
    "skills": {"name", "description", "content", "source", "source_url", "category", "installed"},
    "schedules": {"name", "agent_id", "prompt", "mode", "time", "interval_minutes", "enabled", "archived"},
    "posts": {"title", "body", "channel", "agent_id", "scheduled_at", "media_url", "media_type", "archived"},
}
SETTINGS = {"workspace_name", "owner_name", "execution_enabled", "gateway_url", "image_model", "image", "graph_version", "page_id",
            "telegram_chats", "telegram_agent_id", "telegram_mode", "telegram", "support_agent_id", "messenger_enabled", "demo_data",
            "default_provider", "default_model", "max_parallel", "turns_per_slice", "budget", "quota_policy", "fallback_models", "housekeeping", "skill_sources",
            "session_compact_tokens", "adaptive_effort", "heartbeat", "model_autonomy"}
SECRETS = {"GATEWAY_API_KEY", "FACEBOOK_PAGE_TOKEN", "FACEBOOK_APP_SECRET", "META_VERIFY_TOKEN",
           "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "SEO_WEBHOOK_SECRET"}


class Service:
    def __init__(self, store, providers, vault, engine):
        self.store, self.providers, self.vault, self.engine = store, providers, vault, engine

    def validate(self, kind, data, previous=None):
        if kind=="checklist": return self.engine.progress.validate(data,previous)
        if kind not in FIELDS or not isinstance(data, dict):
            raise DomainError("This operation is not supported.")
        doc = {**(previous or {}), **{k: v for k, v in data.items() if k in FIELDS[kind]}}
        for key, value in doc.items():
            if isinstance(value, str) and len(value) > 100000:
                raise DomainError("Content is too long.")
        name = doc.get("name" if kind in ("agents", "skills", "schedules") else "title", "")
        if not isinstance(name, str) or not name.strip() or len(name) > 250:
            raise DomainError("The name must contain 1 to 250 characters.")
        if kind in ("tasks", "schedules", "posts"):
            agent = self.store.get("agents", doc.get("agent_id", ""))
            if agent.get("archived"):
                raise DomainError("This agent is archived.")
        if kind == "agents":
            if doc.get("face") and doc["face"] not in FACES or doc.get("color") and doc["color"] not in COLORS:
                raise DomainError("Choose a valid face and color.")
            if "model_policy" in data:
                doc["model_policy"] = self.engine.models.normalize_policy(data["model_policy"])
            if doc.get("provider") not in ("codex", "claude", "gemini", "gateway"):
                raise DomainError("Choose a valid provider.")
            if doc.get("status", "idle") not in ("idle", "paused", "working"):
                raise DomainError("Invalid agent status.")
            if not previous and doc.get("status") == "working":
                raise DomainError("Working status is managed by the runtime.")
            if previous and previous["status"] == "working" and any(
                    k in data and data[k] != previous.get(k) for k in ("provider", "model", "tools", "skills")):
                raise DomainError("Stop the current run before changing models or permissions.", 409)
            if not isinstance(doc.get("tools", []), list) or set(doc.get("tools", [])) - TOOLS:
                raise DomainError("Invalid tool list.")
            if not previous:
                doc.setdefault("status", "idle")
                doc.setdefault("avatar", name[0].upper())
                doc.setdefault("color", "purple")
                doc.setdefault("model", "")
                doc.setdefault("skills", [])
                doc.setdefault("tools", ["workspace.read", "memory.write", "team.message"])
                doc.setdefault("heartbeat", True)
                doc["parent_id"] = None
                doc["binding_origin"] = "owner" if doc.get("model") else "default"
        elif kind == "tasks":
            if not doc.get("reviewer_id") or doc["reviewer_id"] == doc["agent_id"]:
                raise DomainError("The reviewer must be different from the task owner.")
            self.store.get("agents", doc["reviewer_id"])
            if not previous:
                doc.setdefault("status", "backlog")
                if doc["status"] not in ("backlog", "ready"):
                    raise DomainError("New tasks must start in Backlog or Ready.")
            elif "status" in data and data["status"] != previous["status"]:
                if data["status"] not in ("backlog", "ready") or previous["status"] in ("running", "done"):
                    raise DomainError("Use Run, Review or Approve for this step.", 409)
            elif previous and previous["status"] == "running":
                raise DomainError("Stop the task before editing it.", 409)
            if doc.get("priority", "medium") not in ("low", "medium", "high"):
                raise DomainError("Invalid priority.")
            for dep in doc.get("dependencies", []):
                if dep == doc.get("id"):
                    raise DomainError("A task cannot depend on itself.")
                self.store.get("tasks", dep)
                pending, seen = [dep], set()
                while pending:
                    current = pending.pop()
                    if current == doc.get("id"):
                        raise DomainError("Dependencies create a cycle. Choose an earlier task.")
                    if current not in seen:
                        seen.add(current)
                        pending.extend(self.store.get("tasks", current).get("dependencies", []))
            doc.setdefault("dependencies", [])
            doc.setdefault("tags", [])
        elif kind == "memories":
            if doc.get("scope") not in ("shared", "private"):
                raise DomainError("Choose a knowledge scope.")
            if doc["scope"] == "private":
                self.store.get("agents", doc.get("agent_id", ""))
            else:
                doc["agent_id"] = None
        elif kind == "skills":
            if not str(doc.get("content", "")).strip():
                raise DomainError("A skill requires SKILL.md content.")
            if not previous or any(k in data for k in ('name','description','content')):
                english_authoring(doc.get('name'),doc.get('description'),doc.get('content'))
            doc["sha256"] = hashlib.sha256(doc["content"].encode()).hexdigest()
            doc.setdefault("installed", True)
            doc.setdefault("source", "Custom")
        elif kind == "schedules":
            if not str(doc.get("prompt", "")).strip():
                raise DomainError("Recurring work needs instructions.")
            if doc.get("mode") not in ("daily", "interval"):
                raise DomainError("Choose a valid schedule type.")
            if not previous or any(k in data and data[k] != previous.get(k) for k in ("mode", "time", "interval_minutes", "enabled")):
                doc["next_run"] = next_schedule(doc)
            doc.setdefault("enabled", False)
        elif kind == "posts":
            if doc.get("channel") not in ("facebook", "blog"):
                raise DomainError("Choose a valid publishing channel.")
            if previous and previous["status"] not in ("draft", "approved"):
                raise DomainError("This post is publishing or published. Create a new draft to make changes.", 409)
            doc["status"] = "draft"
            if doc.get("scheduled_at"):
                try:
                    parsed = parse_time(doc["scheduled_at"])
                    if parsed.tzinfo is None:
                        raise ValueError()
                except (ValueError, TypeError):
                    raise DomainError("The time must include a timezone.") from None
            if doc.get("media_url") and urlparse(doc["media_url"]).scheme != "https":
                raise DomainError("Image and video URLs must use public HTTPS.")
            if doc.get("media_type", "none") not in ("none", "image", "video"):
                raise DomainError("Invalid media type.")
        return {k: v for k, v in doc.items() if k not in ("version", "updated_at")}

    def create(self, kind, data):
        doc=self.validate(kind, data)
        if kind=="agents":
            doc.setdefault("id",uid("a"))
            doc.setdefault("face",face_for(doc["id"]))
        return self.store.create(kind, doc)

    def update(self, kind, record_id, data):
        previous = self.store.get(kind, record_id)
        if "version" not in data or data["version"] != previous["version"]:
            raise DomainError("Data has changed. Reload and try again.", 409)
        doc = self.validate(kind, data, previous)
        team_default = None
        if kind == "agents" and record_id == "a-lead" and any(k in data for k in ("model", "provider", "reasoning_effort")):
            # Atlas always follows the team default: a model typed in its edit form changes the default, never pins Atlas.
            if data.get("model"):
                team_default = self.engine.models.resolve({"provider": doc.get("provider"), "model": data["model"]})
            for key in ("provider", "model", "reasoning_effort", "binding_origin"):
                doc.pop(key, None)
        elif kind == "agents" and any(k in data for k in ("model", "provider")):
            doc["binding_origin"] = "owner" if doc.get("model") else "default"
        result = self.store.patch(kind, record_id, doc, data["version"])
        if team_default:
            self.engine.models.set_default(team_default, source="owner_settings", actor_id="owner")
            result = self.store.get(kind, record_id)
        self.engine.wake_waiters()
        return result

    def normalize_budget(self, value):
        """settings.budget: ints within range; partial updates keep the other limits."""
        from .safety import BUDGET_RANGES
        if not isinstance(value, dict) or set(value) - set(BUDGET_RANGES):
            raise DomainError("Budget accepts only run_turns, project_turns, project_runs and project_tokens.")
        result = self.engine.safety.budget()
        for key, number in value.items():
            low, high = BUDGET_RANGES[key]
            if not isinstance(number, int) or isinstance(number, bool) or not low <= number <= high:
                raise DomainError(f"{key} must be a whole number from {low} to {high}.")
            result[key] = number
        return result

    def normalize_fallbacks(self, value):
        """Ordered fallback models for quota episodes; each must resolve in the current catalog (max 5)."""
        if not isinstance(value, list) or len(value) > 5:
            raise DomainError("Fallback models must be a list of at most 5 entries.")
        result = []
        for item in value:
            if not isinstance(item, dict):
                raise DomainError("Each fallback needs a provider and a model.")
            binding = self.engine.models.resolve({k: item.get(k) for k in ("provider", "model", "reasoning_effort") if item.get(k)})
            if binding not in result:
                result.append(binding)
        return result

    def settings(self, data):
        settings = {k: v for k, v in data.items() if k in SETTINGS}
        if "default_provider" in settings and settings["default_provider"] not in ("codex", "claude", "gemini", "gateway"):
            raise DomainError("Invalid provider.")
        for key, lo, hi in (("max_parallel", 1, 8), ("turns_per_slice", 1, 24)):
            if key in settings and (not isinstance(settings[key], int) or not lo <= settings[key] <= hi):
                raise DomainError("Invalid execution limits.")
        if "session_compact_tokens" in settings and (isinstance(settings["session_compact_tokens"], bool) or not isinstance(settings["session_compact_tokens"], int)
                                                     or not 16000 <= settings["session_compact_tokens"] <= 200000):
            raise DomainError("Session compaction threshold must be between 16000 and 200000 tokens.")
        if "image" in settings:
            settings["image"] = self.engine.images.validate(settings["image"])
        elif str(settings.get("image_model") or "").strip():
            # Legacy single-model setting: keep it working as a fixed gateway engine.
            settings["image"] = self.engine.images.validate({"mode": "fixed", "engine": "gateway", "model": str(settings["image_model"]).strip()})
        if "adaptive_effort" in settings and not isinstance(settings["adaptive_effort"], bool):
            raise DomainError("Invalid configuration state.")
        if "budget" in settings:
            settings["budget"] = self.normalize_budget(settings["budget"])
        before = None
        if "heartbeat" in settings:
            before = self.engine.heartbeat.config()
            settings["heartbeat"] = self.engine.heartbeat.validate(settings["heartbeat"], before)
        if "housekeeping" in settings:
            settings["housekeeping"] = self.engine.housekeeping.validate(settings["housekeeping"], self.store.settings().get("housekeeping"))
        if "skill_sources" in settings:
            settings["skill_sources"] = normalize_sources(settings["skill_sources"])
        if "model_autonomy" in settings and settings["model_autonomy"] not in ("locked", "phases", "full"):
            raise DomainError("Model autonomy must be locked, phases or full.")
        if "quota_policy" in settings and settings["quota_policy"] not in ("ask", "auto_fallback", "wait"):
            raise DomainError("Quota policy must be ask, auto_fallback or wait.")
        if "fallback_models" in settings:
            settings["fallback_models"] = self.normalize_fallbacks(settings["fallback_models"])
        for key in ("execution_enabled", "messenger_enabled", "demo_data"):
            if key in settings and not isinstance(settings[key], bool):
                raise DomainError("Invalid configuration state.")
        if "gateway_url" in settings:
            parsed = urlparse(settings["gateway_url"])
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                raise DomainError("Gateway URL must use HTTP(S) without embedded credentials.")
        if settings.get("page_id") and not re.fullmatch(r"\d+", settings["page_id"]):
            raise DomainError("Page ID must contain digits only.")
        if "graph_version" in settings and not re.fullmatch(r"v\d+\.\d+", settings["graph_version"]):
            raise DomainError("Graph API version must use the format vXX.X.")
        if "telegram_mode" in settings and settings["telegram_mode"] not in ("polling", "webhook"):
            raise DomainError("Telegram connection must be polling or webhook.")
        if "telegram" in settings:
            settings["telegram"] = self.engine.telegram.validate_config(settings["telegram"])
        for key in ("telegram_agent_id", "support_agent_id"):
            if settings.get(key):
                self.store.get("agents", settings[key])
        binding = None
        if "default_model" in settings or "default_provider" in settings:
            previous = self.store.settings()
            binding = self.engine.models.resolve({"provider": settings.get("default_provider", previous.get("default_provider")),
                "model": settings.get("default_model", previous.get("default_model"))})
            # set_default writes the default itself, so it can detect no-ops and record the change.
            settings.pop("default_provider", None)
            settings.pop("default_model", None)
        result = self.store.settings(settings)
        if before is not None:
            self.engine.heartbeat.settings_changed(before, settings["heartbeat"])
            result = self.store.settings()
        if binding:
            self.engine.models.set_default(binding, source="owner_settings")
            result = self.store.settings()
        self.engine.wake_waiters()
        return result

    def action(self, kind, record_id, action, data):
        if kind=='projects' and action=='browser-revoke':return self.engine.browser.revoke(record_id,data)
        if kind == "custom_tools" and action == "archive":
            item = self.store.get("custom_tools",record_id)
            if data.get("version") != item["version"]:
                raise DomainError("The tool has changed. Reload before saving.",409)
            return self.engine.custom_tools.archive({"tool_id":record_id,"archived":data.get("archived",True)})
        if kind == "agents" and action == "model":
            result = self.engine.models.owner_select(record_id, data)
            self.engine.wake_waiters(check_accounts=False)
            return result
        if kind == "projects" and action == "goal-remove":
            return self.engine.goals.remove(record_id, str(data.get("goal_id", "")))
        if kind == "decisions" and action == "answer":
            return self.engine.team.answer(record_id, data)
        if kind == "projects" and action == "folder":
            return self.engine.team.grant_folder(record_id, data)
        if kind == "tasks":
            if action == "run":
                return self.engine.start_task(record_id)
            if action == "review":
                return self.engine.review(record_id)
            task = self.store.get(kind, record_id)
            if action in ("approve", "revise"):
                if task["status"] != "review" or task["agent_id"] == task.get("reviewer_id"):
                    raise DomainError("This task is not ready for review.", 409)
                score = int(data.get("score", 4))
                if not 1 <= score <= 5:
                    raise DomainError("Review scores must be between 1 and 5.")
                updated = self.store.patch(kind, record_id, {"status": "done" if action == "approve" else "ready",
                                "score": score, "review_note": str(data.get("note", ""))[:10000], "reviewed_at": now(),
                                "approved_by": "workspace-owner" if action == "approve" else None})
                self.store.event("Task approved" if action == "approve" else "Changes requested", task["title"], "task")
                if action == "approve":
                    self.store.create("memories", {"title": "Lesson: " + task["title"][:180],
                        "content": f"Review {score}/5. " + str(data.get("note", ""))[:4000] + "\n"
                                   + task.get("review_feedback", "")[:8000],
                        "scope": "private", "agent_id": task["agent_id"], "type": "feedback", "tags": ["Review"]})
                return updated
        if kind == "plans" and action == "approve":
            return self.engine.approve_plan(record_id)
        if kind == "plans" and action == "reject":
            return self.store.patch(kind, record_id, {"status": "rejected"})
        if kind == "runs" and action == "unstick":
            return self.engine.unstick(record_id)
        if kind == "runs" and action == "cancel":
            return self.engine.cancel(record_id, bool(data.get("cascade")))
        if kind == "posts" and action == "approve":
            post = self.store.get(kind, record_id)
            if post["status"] != "draft" or not post.get("body", "").strip():
                raise DomainError("Only non-empty drafts can be approved.", 409)
            if post.get("media_type") in ("image", "video") and not post.get("media_url"):
                raise DomainError("A media URL is required before approving an image or video post.")
            return self.store.patch(kind, record_id, {"status": "approved", "approved_at": now()})
        if kind == "posts" and action == "publish":
            return self.engine.publish(record_id)
        raise DomainError("This operation is not supported.")

    def import_skill(self, data):
        if data.get("url"):
            url = data["url"]
            parsed = urlparse(url)
            if parsed.hostname == "github.com" and "/blob/" in parsed.path:
                url = "https://raw.githubusercontent.com" + parsed.path.replace("/blob/", "/", 1)
                parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname != "raw.githubusercontent.com" or not parsed.path.endswith("SKILL.md"):
                raise DomainError("Enter a GitHub URL pointing to SKILL.md or paste the content directly.")
            try:
                with urlopen(Request(url, headers={"User-Agent": "AgentFlow/0.1"}), timeout=20) as response:
                    if urlparse(response.url).hostname != "raw.githubusercontent.com":
                        raise DomainError("The skill download redirected outside GitHub.")
                    content = response.read(100001).decode("utf-8")
            except (OSError, UnicodeError):
                raise DomainError("Unable to download SKILL.md from this source.", 502) from None
            data = {**data, "content": content, "source_url": url, "source": data.get("source", "GitHub")}
        return self.create("skills", data)

    def scan_skills(self, roots):
        imported = 0
        hashes = {s.get("sha256") for s in self.store.all("skills")}
        for root, source in roots:
            if not root.is_dir():
                continue
            for path in list(root.glob("*/SKILL.md"))[:200]:
                if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > 100000:
                    continue
                text = path.read_text(encoding="utf-8")
                digest = hashlib.sha256(text.encode()).hexdigest()
                if digest in hashes:
                    continue
                name = re.search(r"(?m)^name:\s*[\"']?([^\"'\n]+)", text[:4000])
                description = re.search(r"(?m)^description:\s*(.*)", text[:4000])
                self.create("skills", {"name": name.group(1).strip() if name else path.parent.name,
                    "description": description.group(1).strip()[:500] if description else "Local skill",
                    "content": text, "source": source, "category": "custom", "installed": False})
                hashes.add(digest)
                imported += 1
        return {"imported": imported}
