"""Inherited chat defaults, phase routing and per-run model switching."""
import copy
import re

from .store import DomainError, now


AUTONOMY = ("locked", "phases", "full")
AGENT_SOURCES = ("agent_tool", "quota_switch", "quota_restore")
DONE = ("done", "failed", "cancelled")


def brief(binding):
    """provider/model/reasoning_effort only, for the change log."""
    binding = binding or {}
    return {"provider": binding.get("provider", ""), "model": binding.get("model", ""),
            "reasoning_effort": binding.get("reasoning_effort") or None}


def label(binding):
    binding = binding or {}
    return (binding.get("provider", "?") + "/" + (binding.get("model") or "?")
            + (" " + binding["reasoning_effort"] if binding.get("reasoning_effort") else ""))


class ModelUnavailable(DomainError):
    def __init__(self, model, message=None):
        super().__init__(message or "Model " + model + " is unavailable in the current catalog. Choose an available model in chat to continue.", 409)


class ModelRouting:
    def __init__(self, engine):
        self.engine, self.store, self.providers = engine, engine.store, engine.providers

    def migrate(self):
        if self.store.settings().get("model_routing_version") != 1:
            default = self.engine.default_binding()
            for actor in self.store.all("agents"):
                if all(actor.get(k, "") == default[k] for k in ("provider", "model")) or not actor.get("model"):
                    self.store.patch("agents", actor["id"], {"binding_origin": "default"})
            self.store.settings({"model_routing_version": 1})
        self.follow_default("a-lead")

    def follow_default(self, agent_id):
        """Atlas always follows the team default: an agent-set or pinned model on it is reset (idempotent)."""
        try:
            lead = self.store.get("agents", agent_id)
        except DomainError:
            return
        if lead.get("binding_origin") in ("default", "unconfigured", None):
            return
        default = self.engine.default_binding()
        previous = self.base(lead)
        with self.store.transaction():
            self.store.create("agent_revisions", {"agent_id": agent_id, "snapshot": lead, "changed_by": "system"})
            self.store.patch("agents", agent_id, {**default, "reasoning_effort": default.get("reasoning_effort"),
                                                  "binding_origin": "default", "model_policy": {}})
            self.record("agent_model", default, "migration", previous=previous, agent_id=agent_id,
                        reason="Atlas follows the team default again; its agent-set model and phase policy were cleared.")

    def autonomy(self):
        value = self.store.settings().get("model_autonomy", "phases")
        return value if value in AUTONOMY else "phases"

    def record(self, kind, binding, source, actor_id=None, agent_id=None, previous=None, reason=""):
        event = {"kind": kind, "previous": brief(previous), "binding": brief(binding), "source": source,
                 "actor_id": actor_id, "reason": str(reason or "")[:500]}
        if agent_id:
            event["agent_id"] = agent_id
        return self.store.create("model_events", event)

    def note(self, text):
        """One short activity line in Atlas's DM for team-default changes the owner did not make directly."""
        channel = "dm-a-lead"
        project = next((p for p in reversed(self.store.all("projects")) if p.get("channel") == channel
                        and p["status"] in ("active", "waiting") and not p.get("archived")), None)
        self.store.create("messages", {"agent_id": "a-lead", "role": "assistant", "content": text, "channel": channel,
                                       "project_id": project["id"] if project else None, "run_id": None,
                                       "kind": "progress", "audience": "activity"})

    def history(self, limit=50):
        try:
            limit = max(1, min(200, int(limit)))
        except (TypeError, ValueError):
            limit = 50
        default = brief(self.engine.default_binding())
        events = list(reversed(self.store.all("model_events")))
        diverged = []
        for agent in self.store.all("agents"):
            if agent.get("archived") or agent.get("binding_origin") not in ("agent", "owner"):
                continue
            base = self.base(agent)
            if (base.get("provider"), base.get("model")) == (default["provider"], default["model"]) and \
                    (base.get("reasoning_effort") or None) == default["reasoning_effort"]:
                continue
            last = next((e for e in events if e.get("agent_id") == agent["id"] and e["kind"] == "agent_model"), None)
            diverged.append({"agent_id": agent["id"], "name": agent["name"], "binding": brief(base),
                             "binding_origin": agent["binding_origin"],
                             "set_by": "owner" if agent["binding_origin"] == "owner" else
                             (last or {}).get("actor_id") or agent.get("created_by") or "agent"})
        return {"events": events[:limit], "default": default, "autonomy": self.autonomy(), "diverged": diverged}

    def catalog(self, capability="chat"):
        """Models the owner and agents may select. Entries without `capabilities` are chat models (Codex, Claude,
        gateway); image-only entries (Gemini image models) are listed with capability="image" and never offered for chat."""
        return [{k: m.get(k) for k in ("provider", "id", "name", "resolved_model", "source", "observed_at", "reasoning_efforts", "default_reasoning_effort", "modalities")}
                | {"verified": m["provider"] + ":" + m["id"] in self.providers.verified,
                   "capabilities": list(m.get("capabilities") or ["chat"])}
                for m in self.providers.models if capability in (m.get("capabilities") or ["chat"])]

    def resolve(self, value, provider=None):
        effort = value.get("reasoning_effort") if isinstance(value, dict) else None
        if isinstance(value, dict):
            provider, value = value.get("provider", provider), value.get("model", value.get("id", ""))
        if not isinstance(value, str) or not value.strip():
            raise DomainError("Choose a model from models.list.")
        needle = value.strip().casefold().replace("soinet", "sonnet")
        matches = [m for m in self.catalog() if (not provider or m["provider"] == provider)
                   and needle in (str(m.get(k) or "").casefold() for k in ("id", "name", "resolved_model"))]
        resolved = {(m["provider"], m.get("resolved_model")) for m in matches
                    if needle == str(m.get("resolved_model") or "").casefold()}
        if len(resolved) == 1 and not any(needle == m["id"].casefold() for m in matches):
            selected_provider, selected_model = next(iter(resolved))
            return self.with_effort({"provider": selected_provider, "model": selected_model}, effort, matches[0])
        # Family aliases must resolve through the actual observed catalog.
        if not matches:
            matches = [m for m in self.catalog() if (not provider or m["provider"] == provider)
                       and needle == str(m.get("id", "")).casefold()]
        if len({(m["provider"], m["id"]) for m in matches}) > 1:
            exact = [m for m in matches if needle == m["id"].casefold()]
            matches = exact or matches
        if len(matches) != 1:
            raise DomainError("The model is unavailable or ambiguous in the current catalog: " + value + ". Use models.list and choose an exact provider and ID.")
        return self.with_effort({"provider": matches[0]["provider"], "model": matches[0]["id"]}, effort, matches[0])

    @staticmethod
    def with_effort(binding, effort, entry):
        if effort:
            if effort not in (entry.get("reasoning_efforts") or []):
                raise DomainError("This reasoning level is not listed for the model.")
            binding["reasoning_effort"] = effort
        return binding

    def normalize_policy(self, policy):
        if not isinstance(policy, dict) or set(policy) - {"default", "phases", "initial_phase", "subagents"}:
            raise DomainError("Model policies may contain only default, phases, initial_phase and subagents.")
        result = {}
        if policy.get("default"):
            result["default"] = self.resolve(policy["default"])
        if "phases" in policy:
            if not isinstance(policy["phases"], dict) or len(policy["phases"]) > 24:
                raise DomainError("Provide a valid phase model mapping.")
            result["phases"] = {}
            for phase, model in policy["phases"].items():
                if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", phase):
                    raise DomainError("Phase identifiers must be lowercase, such as analysis, planning or coding.")
                result["phases"][phase] = self.resolve(model)
        phases = result.get("phases", {})
        if policy.get("initial_phase"):
            if policy["initial_phase"] not in phases:
                raise DomainError("The initial phase must exist in phases.")
            result["initial_phase"] = policy["initial_phase"]
        elif phases:
            result["initial_phase"] = next((p for p in ("analysis", "planning") if p in phases), next(iter(phases)))
        if "subagents" in policy:
            child = policy["subagents"]
            if not isinstance(child, dict) or "subagents" in child:
                raise DomainError("Subagent policies contain default, phases and initial_phase, and are inherited by descendants.")
            result["subagents"] = self.normalize_policy(child)
        return result

    def base(self, actor):
        if actor.get("binding_origin") in ("default", "unconfigured") or not actor.get("model"):
            return self.engine.default_binding(actor)
        return {"provider": actor.get("provider", "codex"), "model": actor.get("model", ""),
                **({"reasoning_effort": actor["reasoning_effort"]} if actor.get("reasoning_effort") else {})}

    def initial_phase(self, actor):
        return actor.get("model_policy", {}).get("initial_phase", "chat")

    @staticmethod
    def policy_matches(expected, actual):
        return all(expected.get(k) == actual.get(k) for k in ("provider", "model")) and (
            not expected.get("reasoning_effort") or expected["reasoning_effort"]==actual.get("reasoning_effort"))

    def effective(self, actor, run=None):
        run = run or {}
        policy = actor.get("model_policy", {})
        phase = run.get("model_phase") or self.initial_phase(actor)
        expected = policy.get("phases", {}).get(phase)
        override = run.get("model_override")
        # A quota fallback is a temporary, runtime-chosen override: it may differ from the phase policy.
        if expected and override and not run.get("quota_override") and not self.policy_matches(expected, override):
            raise DomainError("Model for phase " + phase + " must follow the assigned policy.")
        binding = override or expected or policy.get("default") or self.base(actor)
        return {**binding, "phase": phase, "source": "quota" if override and run.get("quota_override") else "run" if override else "phase" if expected else
                "default" if actor.get("binding_origin") == "default" else "agent"}

    def child_config(self, parent, args):
        inherited = copy.deepcopy(parent.get("model_policy", {}).get("subagents", {}))
        if self.autonomy() == "locked":
            # The owner locked models: new agents get the team default plus any owner-assigned subagent policy.
            policy = {**inherited, "subagents": copy.deepcopy(inherited)} if inherited else {}
            return {**self.engine.default_binding(parent), "binding_origin": "default", "model_policy": policy,
                    "inherited_model_policy": inherited}
        policy = self.normalize_policy(args.get("model_policy", {}))
        # Explicit parent instructions apply to all descendant workers.
        if inherited:
            policy = {**policy, **inherited, "subagents": copy.deepcopy(inherited)}
        base = self.resolve(args) if args.get("model") else policy.get("default")
        if args.get("provider") and not args.get("model"):
            raise DomainError("Choose both a provider and a model from the catalog.")
        binding = base or self.engine.default_binding(parent)
        return {**binding, "binding_origin": "agent" if base else "default", "model_policy": policy,
                "inherited_model_policy": inherited}

    def locked(self, what="change models"):
        return DomainError("Model autonomy is locked by the owner: agents cannot " + what
                           + ". Continue with the assigned model and ask the owner in chat if another model is needed.", 403)

    def check_configure(self, actor, target, args):
        mode = self.autonomy()
        if mode == "locked":
            raise self.locked()
        if target.get("binding_origin") == "owner":
            raise DomainError("The owner pinned " + target["name"] + "'s model, so agents cannot change it. "
                              "Use models.switch for this run if you need a different model.", 403)
        if mode == "phases":
            if target["id"] == "a-lead":
                raise DomainError("Atlas follows the team default and agents cannot configure it. Use models.switch for this run.", 403)
            if target["id"] == actor["id"]:
                raise DomainError("Agents cannot reconfigure their own model in phases mode. Use models.switch for this run.", 403)
            if actor["id"] not in (target.get("parent_id"), target.get("created_by")):
                raise DomainError("In phases mode you may configure only agents you created. Use models.switch for this run.", 403)
        elif target["id"] == "a-lead" and (args.get("model") or args.get("provider")):
            raise DomainError("Atlas's base model follows the team default and agents cannot change it. Use models.switch for this run.", 403)

    def configure(self, actor, args):
        if set(args) - {"agent_id", "model", "provider", "inherit", "model_policy", "reason", "reasoning_effort"}:
            raise DomainError("Only model selections can be changed. Accounts, credentials and access remain owner-managed.")
        target = self.store.get("agents", args.get("agent_id", actor["id"]))
        self.check_configure(actor, target, args)
        return self.apply_configure(actor["id"], target, args, "agent", "agent_tool")

    def apply_configure(self, changed_by, target, args, origin, source):
        """Shared by the agent tool (origin "agent") and the owner picker (origin "owner", or "default" for inherit)."""
        changes = {}
        if args.get("inherit"):
            changes.update(self.engine.default_binding(), binding_origin="default", reasoning_effort=None)
        elif args.get("model"):
            changes.update(reasoning_effort=None)
            changes.update(self.resolve(args), binding_origin=origin)
        if "model_policy" in args:
            policy = self.normalize_policy(args["model_policy"])
            required = target.get("inherited_model_policy", {})
            if any(policy.get("phases", {}).get(p) != m for p, m in required.get("phases", {}).items()):
                raise DomainError("Subagents must retain the assigned phase model policy.")
            changes["model_policy"] = policy
        if not changes:
            raise DomainError("Choose a model, inheritance or phase policy to update.")
        previous = self.base(target)
        with self.store.transaction():
            self.store.create("agent_revisions", {"agent_id": target["id"], "snapshot": target, "changed_by": changed_by})
            updated = self.store.patch("agents", target["id"], changes)
            if brief(previous) != brief(self.base(updated)) or updated.get("binding_origin") != target.get("binding_origin") \
                    or "model_policy" in changes:
                self.record("agent_model", self.base(updated), source, actor_id=None if origin != "agent" else changed_by,
                            agent_id=target["id"], previous=previous,
                            reason=args.get("reason") or ("Phase policy updated." if "model" not in changes and not args.get("inherit") else ""))
        return {"agent_id": target["id"], "initial_model": self.base(updated), "model_policy": updated.get("model_policy", {})}

    def selection(self, actor, args, run):
        phase = args.get("phase") or run.get("model_phase") or self.initial_phase(actor)
        if not isinstance(phase, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", phase):
            raise DomainError("Invalid phase.")
        if args.get("model") and self.autonomy() == "locked":
            raise self.locked()
        binding = self.resolve(args) if args.get("model") else None
        expected = actor.get("model_policy", {}).get("phases", {}).get(phase)
        if expected and binding and not args.get("reasoning_effort"):
            binding = {**expected, **binding}
        if expected and binding and not self.policy_matches(expected, binding):
            raise DomainError("Phase " + phase + " must use " + expected["model"] + ".")
        return {"model_phase": phase, "model_override": binding}

    def set_default(self, binding, source="owner_settings", actor_id=None, reason=""):
        binding = self.resolve(binding)
        previous = self.engine.default_binding()
        if brief(previous) == brief(binding):
            # Same model: nothing changes and no agent is reset (only the owner's own Atlas chat pick is dropped).
            if source.startswith("owner") and self.store.get("agents", "a-lead").get("chat_model_override"):
                self.store.patch("agents", "a-lead", {"chat_model_override": None})
                self.engine.wake_waiters(check_accounts=False)
            return binding
        with self.store.transaction():
            self.store.settings({"default_provider": binding["provider"], "default_model": binding["model"],
                                 "default_reasoning_effort": binding.get("reasoning_effort")})
            for actor in self.store.all("agents"):
                if actor["id"] == "a-lead" or actor.get("binding_origin") in ("default", "unconfigured"):
                    self.store.patch("agents", actor["id"], {"reasoning_effort": None, **binding, "binding_origin": "default",
                        **({"chat_model_override": None} if actor["id"] == "a-lead" else {})})
            for run in self.store.all("runs"):
                if run["agent_id"] == "a-lead" and run["status"] not in DONE:
                    self.store.patch("runs", run["id"], {"model_override": None, "model_phase": "chat", "quota_override": None})
            self.record("team_default", binding, source, actor_id=actor_id, previous=previous, reason=reason)
        if source in AGENT_SOURCES:
            self.note("Team default model: " + label(previous) + " \u2192 " + label(binding) + (" (" + reason + ")" if reason else ""))
        self.engine.wake_waiters(check_accounts=False)
        return binding

    def owner_select(self, agent_id, args, source="owner_chat"):
        """The owner's picker. Atlas: scope "chat" (default) changes only Atlas's chat, "team" changes the team default.
        Other agents: the pick pins that agent (binding_origin "owner"); inherit returns it to the team default."""
        actor = self.store.get("agents", agent_id)
        scope = args.get("scope") or "chat"
        if scope not in ("chat", "team"):
            raise DomainError("Scope must be chat or team.")
        if agent_id == "a-lead" and scope == "team":
            return {**self.set_default({k: args.get(k) for k in ("provider", "model", "reasoning_effort") if args.get(k)}, source=source),
                    "scope": "team"}
        if agent_id == "a-lead":
            binding = None if args.get("inherit") else self.resolve(args, args.get("provider"))
            previous = actor.get("chat_model_override") or self.base(actor)
            result = {**(binding or self.base(actor)), "scope": "chat"}
        else:
            previous = self.base(actor)
            result = self.apply_configure("owner", actor, {"agent_id": agent_id, "inherit": args.get("inherit", False),
                "provider": args.get("provider"), "model": args.get("model"), "reasoning_effort": args.get("reasoning_effort")},
                "owner", source)
            binding = None if args.get("inherit") else self.resolve(args, args.get("provider"))
            result = {**result, "scope": "chat"}
        self.store.patch("agents", agent_id, {"chat_model_override": binding})
        for run in self.store.all("runs"):
            if run["agent_id"] == agent_id and run["status"] not in DONE:
                self.store.patch("runs", run["id"], {"model_override": binding, "model_phase": "chat", "quota_override": None})
        if agent_id == "a-lead" and brief(previous) != brief(binding or self.base(actor)):
            self.record("agent_model", binding or self.base(actor), source, agent_id=agent_id, previous=previous,
                        reason="Atlas chat only; the team default is unchanged.")
        return result

    def owner_command(self, agent_id, content):
        text = content.strip()
        if text.casefold() in ("/model", "/models"):
            return "Model hiện có:\n" + "\n".join(m["provider"] + "/" + m["id"] +
                (" → " + m["resolved_model"] if m.get("resolved_model") else "") for m in self.catalog())
        command = re.fullmatch(r"/model\s+(.+)", text, re.IGNORECASE)
        natural = re.fullmatch(r"(?:dùng|chọn|đặt|đổi sang|set|use)\s+(.+?)\s+(?:làm\s+|as\s+)?(?:model\s+)?(?:mặc định|default)(?:\s+(?:cho\s+)?(?:cả\s+)?(?:đội|team|atlas))?[.!]?", text, re.IGNORECASE)
        match = command or natural
        if not match:
            return None
        value = match[1].strip()
        if value.casefold() in ("inherit", "kế thừa"):
            self.owner_select(agent_id, {"inherit": True}, source="owner_command")
            return ("Đã bỏ lựa chọn riêng cho cuộc trò chuyện này; Atlas dùng model mặc định của đội." if agent_id == "a-lead" else
                    "Đã trở về model mặc định của đội. Agent vẫn có thể chuyển model theo nhiệm vụ.")
        provider = None
        if "/" in value and value.split("/", 1)[0] in ("codex", "claude", "gemini", "gateway"):
            provider, value = value.split("/", 1)
        binding = self.resolve(value, provider)
        self.owner_select(agent_id, {**binding, "scope": "team"}, source="owner_command")
        self.engine.wake_waiters(check_accounts=False)
        return "Đã chọn " + binding["provider"] + "/" + binding["model"] + (" làm model mặc định của cả đội (mọi agent đang theo mặc định cũng dùng model này)." if agent_id == "a-lead" else " làm model ban đầu cho cuộc trò chuyện này (đã ghim cho agent này).") + " Agent có thể chuyển model theo từng giai đoạn."

    def invoke(self, name, args, actor, run):
        if name == "models.list":
            errors = {}
            if args.get("refresh"):
                names = [args["provider"]] if args.get("provider") else [p for p in ("codex", "claude", "gemini") if self.providers.cli.get(p)]
                for provider in names:
                    try:
                        self.providers.refresh_models(provider)
                    except DomainError as exc:
                        errors[provider] = str(exc)
            return {"models": self.catalog(), "current": self.effective(actor, run), "errors": errors}
        if name == "models.configure":
            return self.configure(actor, args)
        if name == "models.switch":
            if self.autonomy() == "locked":
                raise self.locked()
            changes = self.selection(actor, args, run)
            current = self.store.patch("runs", run["id"], {**changes, "quota_override": None, "model_switch_reason": str(args.get("reason", ""))[:1500]})
            return {"next_turn": self.effective(actor, current), "checkpoint_preserved": True}
        if name == "models.set_default":
            if self.autonomy() == "locked":
                raise self.locked("change the team default")
            # A genuine new owner chat is required; files and peer messages cannot change the team default.
            messages = [m for m in self.store.all("messages") if m.get("run_id") == run["id"] and m["role"] == "user"]
            text = messages[-1]["content"].casefold().replace("soinet", "sonnet") if messages else ""
            binding = self.resolve(args, args.get("provider"))
            model = binding["model"].casefold()
            requested = bool(re.search(r"\b(?:dùng|chọn|đổi|đặt|set|use|select|switch)\b", text))
            uncertain = bool(re.search(r"\b(?:không|đừng|chưa|nếu)\s+(?:(?:muốn|cần|hãy|vội|nên)\s+)?(?:dùng|chọn|đổi|đặt|set|use|select|switch)\b|^(?:có thể|có nên|liệu|should|can|does|is|would|could)\b", text))
            if (actor["id"] != "a-lead" or run.get("kind") != "chat" or
                    not requested or uncertain or not any(s in text for s in ("mặc định", "default", "/model")) or model not in text):
                raise DomainError("Only an explicit model selection or owner request in Atlas chat can change the team default.", 403)
            return self.set_default(binding, source="agent_tool", actor_id=actor["id"],
                                    reason="Atlas applied an explicit owner request")
        raise DomainError("Model tool not supported.")
