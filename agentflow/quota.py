"""Team-wide handling of provider usage limits ("quota episodes").

One episode exists per (provider, account fingerprint). While it is active:
* runs bound to that provider/account are parked as `waiting_quota` by `pump()` without spawning a CLI,
* agents on other providers keep working,
* the owner gets exactly one non-blocking decision in Atlas chat (wait / switch / cancel),
* the episode ends when the reset time passes, a probe succeeds, the account changes or work is cancelled.

Unknown reset times never block forever: probes at 10, 20, 40, then every 60 minutes (usage data is
consulted first, so a probe only spends a real request when nothing better is known).
"""
import json
import re
import time
from datetime import datetime, timedelta, timezone

from .store import DomainError, now, uid
from .safety import DEFAULT_BUDGET  # noqa: F401  (settings defaults are seeded here)

GMT7 = timezone(timedelta(hours=7))
RESET_PAD = 30                       # seconds after a known reset before resuming
PROBE_DELAYS = (600, 1200, 2400, 3600)
SHORT_DELAYS = (30, 120, 300)        # transient rate limit / overload, max 3 attempts
USAGE_FRESH = 300
NAMES = {"codex": "Codex", "claude": "Claude", "gateway": "Gateway", "gemini": "Gemini"}
ACTIONS = ("wait", "switch_agent", "switch_all", "cancel")
LIVE = ("queued", "running", "waiting_quota")

_CAP = ("usage limit", "session limit", "weekly limit", "five_hour", "seven_day", "5-hour", "hit your", "quota",
        "credit", "resets", "plan limit", "monthly limit", "daily limit")
_SHORT = ("rate limit", "rate_limit", "overloaded", "too many requests", "529", "429", "temporarily unavailable")
_TRY_AGAIN = re.compile(r"try again in\s+(\d+(?:\.\d+)?)\s*(ms|milliseconds?|s|secs?|seconds?|m|mins?|minutes?)\b")


class TransientLimit(DomainError):
    """A short rate limit or overload; retried with 30s / 2m / 5m backoff, never a quota episode."""
    def __init__(self, provider, detail=""):
        super().__init__(NAMES.get(provider, provider) + " is rate-limiting or temporarily overloaded.", 429)
        self.provider = provider
        self.detail = str(detail)[:2000]


def is_usage_cap(exc, exhausted=False):
    """Usage caps (session/weekly/plan) wait for a reset; everything else short-lived is retried quickly."""
    if exhausted or getattr(exc, "retry_at", None):
        return True
    text = str(getattr(exc, "detail", "") or "").casefold()
    again = _TRY_AGAIN.search(text)
    if again:
        amount, unit = float(again[1]), again[2]
        seconds = amount / 1000 if unit.startswith("mil") or unit == "ms" else amount * 60 if unit.startswith("m") else amount
        if seconds <= 120:
            return False
    if any(word in text for word in _CAP):
        return True
    return not any(word in text for word in _SHORT)


def fmt_time(ts):
    moment = datetime.fromtimestamp(ts, GMT7)
    text = moment.strftime("%H:%M")
    if moment.date() != datetime.fromtimestamp(time.time(), GMT7).date():
        text += moment.strftime(" %d/%m")
    return text


class QuotaManager:
    def __init__(self, engine):
        self.engine, self.store, self.providers = engine, engine.store, engine.providers
        self.path = self.store.directory / "quota-episodes.json"
        self.episodes = {}
        self.default_restore = None   # {previous, set, episodes:[ids]} while a switch_all is active
        self._load()
        engine.quota = self
        self.providers.quota = self
        engine.decision_handlers["quota"] = self.answer
        settings = self.store.settings()
        defaults = {"quota_policy": "ask", "fallback_models": [], "budget": dict(DEFAULT_BUDGET)}
        missing = {k: v for k, v in defaults.items() if k not in settings}
        if missing:
            self.store.settings(missing)

    # ---- persistence ---------------------------------------------------------------

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.episodes = {e["id"]: e for e in data.get("episodes", []) if isinstance(e, dict) and e.get("id")}
            self.default_restore = data.get("default_restore")
        except (OSError, ValueError, TypeError, AttributeError):
            self.episodes, self.default_restore = {}, None

    def _save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"episodes": list(self.episodes.values()), "default_restore": self.default_restore}), encoding="utf-8")
        temporary.replace(self.path)

    # ---- helpers -------------------------------------------------------------------

    def policy(self):
        value = self.store.settings().get("quota_policy", "ask")
        return value if value in ("ask", "auto_fallback", "wait") else "ask"

    def fingerprint(self, provider):
        return (self.providers.accounts.current.get(provider) or {}).get("fingerprint") or ""

    def name(self, provider):
        return NAMES.get(provider, provider)

    def find(self, provider, fingerprint):
        for episode in self.episodes.values():
            if episode["provider"] == provider and (not fingerprint or not episode["fingerprint"] or episode["fingerprint"] == fingerprint):
                return episode
        return None

    def _new(self, provider, fingerprint):
        episode = {"id": uid("qe"), "provider": provider, "fingerprint": fingerprint, "started_at": time.time(),
                   "retry_at": 0, "known": False, "failures": 0, "phase": "waiting", "probe_run": None,
                   "decision_id": None, "answered": False, "switched_default": False}
        self.episodes[episode["id"]] = episode
        self.store.event(self.name(provider) + " usage limit reached",
                         "Agents on this account are paused; other providers keep working.", "system")
        self._refresh_others(episode)   # a fallback on this provider is no longer usable
        return episode

    def _refresh_others(self, episode):
        for other in list(self.episodes.values()):
            if other["id"] != episode["id"]:
                self._refresh(other)

    def members(self, episode):
        return [r for r in self.store.all("runs") if r["status"] == "waiting_quota" and r.get("quota_episode") == episode["id"]]

    def usage_state(self, provider):
        """What the last observed usage windows say (only trusted when fresh)."""
        try:
            windows = self.providers.usage.public(provider).get("windows", [])
        except Exception:
            windows = []
        live = [w for w in windows if not w.get("expired")]
        exhausted = [w for w in live if w.get("status") == "rejected" or (w.get("used_percent") or 0) >= 100]
        resets = [w["resets_at"] for w in exhausted if w.get("resets_at")]
        fresh = bool(live) and all(not w.get("stale") for w in live)
        return {"fresh": fresh, "clear": fresh and not exhausted, "exhausted": bool(exhausted),
                "reset": max(resets) if resets else None}

    def window_reset(self, provider):
        return self.usage_state(provider)["reset"]

    def action_text(self, run):
        episode = self.episodes.get(run.get("quota_episode"))
        if not episode:
            return "Waiting for account quota"
        name = self.name(episode["provider"])
        if episode["phase"] == "probing":
            return "Waiting for " + name + " quota · testing access"
        return "Waiting for " + name + " quota · " + ("resumes " if episode["known"] else "next check ") + fmt_time(episode["retry_at"])

    def describe(self, episode):
        name = self.name(episode["provider"])
        if episode["known"]:
            return name + " usage limit reached. Work keeps its checkpoint and resumes automatically around " + fmt_time(episode["retry_at"]) + " (GMT+7)."
        return (name + " usage limit reached and the reset time is unknown. Work keeps its checkpoint; AgentFlow checks again around "
                + fmt_time(episode["retry_at"]) + " (GMT+7) and backs off up to 60 minutes.")

    def _mark_agent(self, run, episode):
        try:
            agent = self.store.get("agents", run["agent_id"])
        except DomainError:
            return
        if agent["status"] != "paused" and run["agent_id"] not in self.engine.active:
            self.store.patch("agents", agent["id"], {"current_action": self.action_text({**run, "quota_episode": episode["id"]})})

    # ---- planning (called by Providers.infer and the execute() except branch) ----------

    def plan(self, provider, fingerprint, exc):
        """Give a usage-limit error its final `retry_at` (never None) and record the episode."""
        with self.store.lock:
            t = time.time()
            episode = self.find(provider, fingerprint)
            if getattr(exc, "planned", False) and exc.retry_at:
                if not episode:
                    episode = self._new(provider, fingerprint)
                    episode.update(retry_at=exc.retry_at, known=True)
                    self._save()
                exc.episode_id = episode["id"]
                return episode
            reset = exc.retry_at or self.window_reset(provider)
            episode = episode or self._new(provider, fingerprint)
            if reset and reset + RESET_PAD > t:
                retry = reset + RESET_PAD
                if episode["known"] and episode["retry_at"] > t:
                    retry = max(retry, episode["retry_at"])
                episode.update(known=True, failures=0)
            elif episode["known"] and episode["retry_at"] > t:
                retry = episode["retry_at"]
            elif episode["retry_at"] > t and episode["phase"] == "waiting":
                retry = episode["retry_at"]        # a run that was already in flight joins the wait
            else:
                failures = episode["failures"]
                retry = t + PROBE_DELAYS[min(failures, len(PROBE_DELAYS) - 1)]
                episode.update(failures=failures + 1, known=False)
            moved = abs(retry - episode["retry_at"]) > 1
            episode.update(retry_at=retry, phase="waiting", probe_run=None)
            exc.retry_at, exc.planned, exc.episode_id = retry, True, episode["id"]
            if moved:
                self._retime(episode)
            self._sync_blocks(episode)
            self._save()
            return episode

    def _sync_blocks(self, episode):
        changed = False
        for (provider, fingerprint), block in list(self.providers.quota_blocks.items()):
            if provider == episode["provider"] and (not fingerprint or not episode["fingerprint"] or fingerprint == episode["fingerprint"]):
                if block.retry_at != episode["retry_at"]:
                    block.retry_at, changed = episode["retry_at"], True
        if changed:
            self.providers._save_quota_blocks()

    def _retime(self, episode):
        for run in self.members(episode):
            self.store.patch("runs", run["id"], {"retry_at": episode["retry_at"]})
            self._mark_agent(run, episode)

    # ---- run lifecycle ------------------------------------------------------------

    def on_limit(self, run, exc):
        """execute() caught a ProviderLimit: park the run, or move it to a fallback when policy allows."""
        with self.engine.guard, self.store.lock:
            run = self.store.get("runs", run["id"])
            if run["status"] == "cancelled":
                return
            selected = self.engine.models.effective(self.store.get("agents", run["agent_id"]), run)
            episode = self.plan(selected["provider"], self.fingerprint(selected["provider"]), exc)
            self.engine.safety.refund(run)
            if self.policy() == "auto_fallback":
                fallback = (self.fallbacks(episode) or [None])[0]
                if fallback:
                    self.apply_override(run, episode, fallback, auto=True)
                    self._refresh(episode)
                    return
            self.block(run, episode, selected)

    def gate(self, run):
        """pump(): True when this queued run must not start because its provider/account is paused."""
        if not self.episodes:
            return False
        with self.engine.guard, self.store.lock:
            try:
                selected = self.engine.models.effective(self.store.get("agents", run["agent_id"]), run)
            except DomainError:
                return False
            if not selected.get("model"):
                return False
            episode = self.find(selected["provider"], self.fingerprint(selected["provider"]))
            if not episode:
                return False
            if episode["phase"] == "probing" and episode["probe_run"] == run["id"]:
                return False
            if episode["phase"] == "waiting" and time.time() >= episode["retry_at"]:
                return False
            if self.policy() == "auto_fallback":
                fallback = (self.fallbacks(episode) or [None])[0]
                if fallback:
                    self.apply_override(run, episode, fallback, auto=True, requeue=False)
                    self._refresh(episode)
                    return False
            self.block(run, episode, selected)
            return True

    def block(self, run, episode, selected):
        t = time.time()
        run = self.store.patch("runs", run["id"], {
            "status": "waiting_quota", "error": self.describe(episode), "retry_at": episode["retry_at"],
            "quota_blocked_at": t, "quota_binding": {k: selected[k] for k in ("provider", "model")},
            "quota_account": self.fingerprint(selected["provider"]) or None,
            "quota_episode": episode["id"], "quota_provider": episode["provider"]})
        self._mark_agent(run, episode)
        if run.get("quota_notified") != episode["id"]:
            self.store.patch("runs", run["id"], {"quota_notified": episode["id"]})
            self.engine.team.emit(run["agent_id"], self.describe(episode), run, "progress")
        self._refresh(episode)

    def apply_override(self, run, episode, fallback, auto=False, requeue=True):
        binding = {k: fallback[k] for k in ("provider", "model", "reasoning_effort") if fallback.get(k)}
        changes = {"model_override": binding, "quota_override": episode["id"], "error": "",
                   "quota_prev_override": run.get("model_override") if not run.get("quota_override") else run.get("quota_prev_override"),
                   "resume_reason": self.name(episode["provider"]) + " reached its usage limit. Continuing from the same checkpoint on "
                                    + binding["provider"] + "/" + binding["model"] + "."}
        if requeue:
            changes["status"] = "queued"
        run = self.store.patch("runs", run["id"], changes)
        self.engine.team.emit(run["agent_id"], self.name(episode["provider"]) + " reached its usage limit"
                              + (" - switched automatically to " if auto else " - switched to ") + binding["provider"] + "/" + binding["model"]
                              + " for this task. New work uses the normal model again once the limit resets.", run, "progress")

    def should_resume(self, run):
        """wake_waiters(): True when a `waiting_quota` run may be requeued now."""
        try:
            selected = self.engine.models.effective(self.store.get("agents", run["agent_id"]), run)
        except DomainError:
            return False
        binding = {k: selected[k] for k in ("provider", "model")}
        if binding != run.get("quota_binding"):
            return True
        account = self.providers.accounts.current.get(binding["provider"], {})
        if account.get("signed_in") and run.get("quota_account") and account.get("fingerprint") != run["quota_account"]:
            return True
        episode = self.episodes.get(run.get("quota_episode"))
        if episode is None:
            return self._adopt(run, binding)
        if run.get("quota_override"):
            original = self.engine.models.effective(self.store.get("agents", run["agent_id"]),
                {**run, "model_override": run.get("quota_prev_override"), "quota_override": None})
            if original.get("model") and not self.find(original["provider"], self.fingerprint(original["provider"])):
                self.store.patch("runs", run["id"], {"model_override": run.get("quota_prev_override"), "quota_override": None})
                return True
        if episode["phase"] == "probing":
            return episode["probe_run"] == run["id"]
        return False

    def _adopt(self, run, binding):
        """A waiting run from before this feature (or whose episode was lost): never wait without a schedule."""
        t = time.time()
        retry = run.get("retry_at")
        if run.get("quota_episode") or (retry and retry <= t):
            return True                               # its episode ended, or a legacy reset time passed
        with self.store.lock:
            provider, fingerprint = binding["provider"], run.get("quota_account") or self.fingerprint(binding["provider"])
            episode = self.find(provider, fingerprint) or self._new(provider, fingerprint)
            if retry:
                episode.update(known=True, retry_at=max(episode["retry_at"], retry))
            elif episode["retry_at"] <= t:
                episode.update(known=False, retry_at=t + PROBE_DELAYS[0], failures=max(episode["failures"], 1))
            self.store.patch("runs", run["id"], {"quota_episode": episode["id"], "retry_at": episode["retry_at"], "quota_provider": provider})
            self._save()
        return False

    def on_success(self, provider, fingerprint):
        with self.store.lock:
            episode = self.find(provider, fingerprint)
            if episode and episode["phase"] == "probing":
                self.end(episode, "A request succeeded.")

    # ---- episode progression -------------------------------------------------------

    def sweep(self):
        if not self.episodes:
            return
        with self.store.lock:
            runs = self.store.all("runs")
            for episode in list(self.episodes.values()):
                if episode["id"] in self.episodes:
                    self._advance(episode, runs, time.time())

    def _advance(self, episode, runs, t):
        provider = episode["provider"]
        members = [r for r in runs if r["status"] == "waiting_quota" and r.get("quota_episode") == episode["id"]]
        account = self.providers.accounts.current.get(provider) or {}
        if account.get("signed_in") and account.get("fingerprint") and episode["fingerprint"] and account["fingerprint"] != episode["fingerprint"]:
            return self.end(episode, "The account changed.")
        if episode["phase"] == "waiting":
            if not episode["known"]:
                reset = self.usage_state(provider)["reset"]
                if reset and reset + RESET_PAD > t:
                    episode.update(known=True, retry_at=reset + RESET_PAD, failures=0)
                    self._retime(episode)
                    self._sync_blocks(episode)
                    self._save()
            if t >= episode["retry_at"]:
                if episode["known"]:
                    return self.end(episode, "The reset time passed.")
                state = self.usage_state(provider)
                if state["clear"]:
                    return self.end(episode, "Fresh usage data shows capacity again.")
                if state["exhausted"] and state["reset"] and state["reset"] + RESET_PAD > t:
                    episode.update(known=True, retry_at=state["reset"] + RESET_PAD, failures=0)
                    self._retime(episode)
                    self._sync_blocks(episode)
                    self._save()
                    return
                if not members:
                    return self.end(episode, "Nothing is waiting.")
                episode.update(phase="probing", probe_run=members[0]["id"])
                self._save()
            elif not members:
                self._close_decision(episode, "No work is waiting on this limit any more.", "cancelled")
        elif episode["phase"] == "probing":
            probe = next((r for r in runs if r["id"] == episode["probe_run"]), None)
            if not probe or probe["status"] not in LIVE:
                if members:
                    episode["probe_run"] = members[0]["id"]
                    self._save()
                else:
                    self.end(episode, "The test request is finished.")

    def end(self, episode, reason):
        with self.store.lock:
            if self.episodes.pop(episode["id"], None) is None:
                return
            for key in [k for k in self.providers.quota_blocks if k[0] == episode["provider"]
                        and (not k[1] or not episode["fingerprint"] or k[1] == episode["fingerprint"])]:
                self.providers.quota_blocks.pop(key, None)
            self.providers._save_quota_blocks()
            self._close_decision(episode, "Quota is available again. " + reason + " Work resumes automatically.", "answered")
            self.store.event(self.name(episode["provider"]) + " usage available again", reason, "system")
            self._save()
            if episode.get("switched_default"):
                self._restore_default(episode)
            self._refresh_others(episode)   # fallbacks on this provider are usable again

    def _close_decision(self, episode, text, status):
        if not episode.get("decision_id"):
            return
        try:
            decision = self.store.get("decisions", episode["decision_id"])
        except DomainError:
            return
        if decision["status"] == "open":
            self.store.patch("decisions", decision["id"], {"status": status, "answer": text, "answered_at": now()})

    # ---- fallbacks -----------------------------------------------------------------

    def candidates(self, provider):
        configured = [c for c in (self.store.settings().get("fallback_models") or []) if isinstance(c, dict)]
        if configured:
            return configured
        found = []
        settings = self.store.settings()
        for other in ("codex", "claude", "gemini"):
            if other == provider:
                continue
            if settings.get("default_provider") == other and settings.get("default_model"):
                found.append(self.engine.default_binding())
                continue
            entry = next((m for m in self.providers.models if m["provider"] == other), None)
            if entry:
                found.append({"provider": other, "model": entry["id"]})
        return found

    def fallbacks(self, episode):
        """Usable fallback bindings: other provider/account, no active episode, present in the catalog."""
        result = []
        for item in self.candidates(episode["provider"]):
            provider, model = item.get("provider"), item.get("model")
            if not provider or not model or provider == episode["provider"]:
                continue
            if self.find(provider, self.fingerprint(provider)):
                continue
            entry = next((m for m in self.providers.models if m["provider"] == provider and model in (m["id"], m.get("resolved_model"))), None)
            if not entry:
                continue
            binding = {"provider": provider, "model": entry["id"], "name": entry.get("name") or entry["id"]}
            if item.get("reasoning_effort") in (entry.get("reasoning_efforts") or []):
                binding["reasoning_effort"] = item["reasoning_effort"]
            result.append(binding)
        return result

    # ---- owner decision ------------------------------------------------------------

    def _refresh(self, episode):
        """Create (once) or update the single owner decision for this episode."""
        members = self.members(episode)
        if not members or episode["answered"] or self.policy() == "wait":
            return
        fallback = (self.fallbacks(episode) or [None])[0]
        provider = self.name(episode["provider"])
        wait_label = ("Wait — auto-resume at " + fmt_time(episode["retry_at"])) if episode["known"] else "Wait — check again every 10–60 min"
        actions = [{"value": "wait", "label": wait_label, "style": "primary",
                    "hint": "Paused work keeps its checkpoint. Agents on other providers keep working."}]
        if fallback:
            label = fallback["name"]
            actions += [{"value": "switch_agent", "label": "Switch blocked agents to " + label + " for these tasks", "style": "quiet",
                         "hint": fallback["provider"] + "/" + fallback["model"] + " · only for the blocked runs"},
                        {"value": "switch_all", "label": "Use " + label + " as team default until " + provider + " resets", "style": "quiet",
                         "hint": "The previous default is restored automatically."}]
        actions.append({"value": "cancel", "label": "Cancel blocked work", "style": "danger"})
        names = list(dict.fromkeys(self._agent_name(r) for r in members))
        question = (provider + " usage limit reached. " + str(len(members)) + (" task is" if len(members) == 1 else " tasks are") + " paused: "
                    + ", ".join(names[:6]) + ("…" if len(names) > 6 else "") + ".")
        context = (self.describe(episode) + "\nAgents on other providers keep working."
                   + ("\nA fallback is available: " + fallback["provider"] + "/" + fallback["model"] + "." if fallback else "\nNo fallback model is available right now."))
        decision = None
        if episode.get("decision_id"):
            try:
                decision = self.store.get("decisions", episode["decision_id"])
            except DomainError:
                decision = None
        if decision and decision["status"] == "answered":
            episode["answered"] = True
            self._save()
            return
        if decision and decision["status"] == "open":
            if (decision["question"], decision["context"], decision.get("actions")) != (question, context, actions):
                self.store.patch("decisions", decision["id"], {"question": question, "context": context, "actions": actions,
                                                                "options": [{"value": a["label"], "label": a["label"]} for a in actions]})
                for message in self.store.all("messages"):
                    if message.get("decision_id") == decision["id"] and message.get("kind") == "decision":
                        self.store.patch("messages", message["id"], {"content": question})
            return
        rid = uid("run")
        carrier = self.store.create("runs", {"id": rid, "agent_id": "a-lead", "prompt": "Tell the owner about the provider usage limit.",
            "kind": "quota_notice", "status": "done", "steps": 0, "trace": [], "phase": "model", "pending_calls": [],
            "wait_for_runs": [], "wait_for_tasks": [], "wait_for_decisions": [], "root_id": rid, "channel": "dm-a-lead",
            "owner_channel": "dm-a-lead", "contract_version": 2, "retry_count": 0, "system_managed": True, "completed_at": now()})
        asked = self.engine.team.ask(self.store.get("agents", "a-lead"), {"question": question, "context": context,
            "options": [a["label"] for a in actions], "blocking": False, "actions": actions,
            "action_field": "quota_action", "action_handler": "quota"}, carrier, _routed=True)
        self.store.patch("decisions", asked["decision_id"], {"quota_episode": episode["id"], "quota_provider": episode["provider"]})
        episode["decision_id"] = asked["decision_id"]
        self._save()

    def _agent_name(self, run):
        try:
            return self.store.get("agents", run["agent_id"])["name"]
        except DomainError:
            return run["agent_id"]

    def answer(self, question, answer):
        """Decision handler for field `quota_action`."""
        value = answer.get("quota_action")
        with self.store.lock:
            episode = self.episodes.get(question.get("quota_episode"))
            if not episode:
                return
            members = self.members(episode)
            if value in ("switch_agent", "switch_all"):
                fallback = (self.fallbacks(episode) or [None])[0]
                if not fallback:
                    raise DomainError("No usable fallback model is available right now. Choose Wait or Cancel.", 409)
                if value == "switch_agent":
                    for run in members:
                        self.apply_override(run, episode, fallback)
                else:
                    self._switch_default(episode, fallback)
            elif value == "cancel":
                for run in members:
                    self.engine.cancel(run["id"])
            episode["answered"] = True
            self._save()

    def _switch_default(self, episode, fallback):
        previous = self.store.settings()
        if self.default_restore is None:
            self.default_restore = {"episodes": [], "set": None, "previous": (
                {k: previous.get(k2) for k, k2 in (("provider", "default_provider"), ("model", "default_model"),
                                                  ("reasoning_effort", "default_reasoning_effort"))}
                if previous.get("default_model") else None)}
        binding = self.engine.models.set_default({k: fallback[k] for k in ("provider", "model", "reasoning_effort") if fallback.get(k)},
            source="quota_switch", reason="quota fallback; restores when " + self.name(episode["provider"]) + " resets")
        self.default_restore["set"] = binding
        if episode["id"] not in self.default_restore["episodes"]:
            self.default_restore["episodes"].append(episode["id"])
        episode["switched_default"] = True
        self.store.event("Team default model switched", "Using " + binding["provider"] + "/" + binding["model"]
                         + " until " + self.name(episode["provider"]) + " resets.", "system")

    def _restore_default(self, episode):
        state = self.default_restore
        if not state:
            return
        state["episodes"] = [e for e in state["episodes"] if e != episode["id"]]
        if state["episodes"]:
            self._save()
            return
        self.default_restore = None
        self._save()
        current, applied, previous = self.engine.default_binding(), state.get("set") or {}, state.get("previous")
        if not previous or not previous.get("model"):
            return
        if (current.get("provider"), current.get("model")) != (applied.get("provider"), applied.get("model")):
            return                                    # the owner chose another default meanwhile
        try:
            binding = self.engine.models.set_default({k: v for k, v in previous.items() if v},
                source="quota_restore", reason="quota reset; previous default restored")
        except DomainError:
            return
        self.store.event("Restored default model " + binding["provider"] + "/" + binding["model"],
                         "The team default is back to its previous setting.", "system")
