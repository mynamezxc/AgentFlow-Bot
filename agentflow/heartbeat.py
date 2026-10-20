"""Team heartbeat: loop cadence, active hours for scheduled work and the token-frugal team pulse.

Settings live in `settings.heartbeat` (validated by `Heartbeat.validate`); the pulse bookkeeping lives in
`settings.heartbeat_state` (runtime-owned, never writable through POST /settings).

Rules this module keeps:
  * Only scheduled/pulse work is ever paused by quiet hours. Owner chat and work that already runs are not touched.
  * The pulse digest is computed without a model call. A model run is enqueued only when something is open and
    (unless forced) the digest changed since the last pulse.
  * At most one pulse run is non-terminal at any time.
"""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from .contract import TERMINAL
from .language import require_english
from .schedule import VN, parse_time
from .store import DomainError, now

DEFAULT_PULSE_PROMPT = (
    "Team pulse. Review the digest below and keep the team moving. Unblock or reassign stalled or blocked work with "
    "the tasks and team tools (tasks.inspect, tasks.delegate, team.message); do not redo teammates' work and do not "
    "start new projects. Message the owner only when a decision or an important result needs them (chat.send with "
    "audience owner, or decisions.ask). Otherwise finish silently with a short English summary in `summary`.")

DEFAULTS = {
    "interval_seconds": 30,
    "active_hours": {"enabled": False, "start": "08:00", "end": "22:00"},
    "active_days": [1, 2, 3, 4, 5, 6, 7],
    "pause_schedules_outside_hours": True,
    "pulse": {"enabled": False, "agent_id": "a-lead", "every_minutes": 60, "only_if_changed": True,
              "notify": "needed", "prompt": DEFAULT_PULSE_PROMPT},
}
RANGES = {"interval_seconds": (10, 300), "every_minutes": (15, 1440)}
TIME = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
OPEN_TASK = ("ready", "running", "review", "blocked", "failed")
OWNER_WAIT = ("waiting_user", "blocked_model", "blocked_account")
FAILED_WINDOW = timedelta(days=7)


def _int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def minutes_of(text):
    hour, minute = TIME.fullmatch(text).groups()
    return int(hour) * 60 + int(minute)


def window(cfg):
    """(start, end, overnight) in minutes. start == end means the whole day."""
    hours = cfg["active_hours"]
    start, end = minutes_of(hours["start"]), minutes_of(hours["end"])
    if start == end:
        return 0, 1440, False
    return start, end, end < start


def active_at(cfg, at):
    """Is `at` inside the configured active window (GMT+7)? A window belongs to the day it starts on."""
    if not cfg["active_hours"]["enabled"]:
        return True
    local = at.astimezone(VN)
    start, end, overnight = window(cfg)
    minute = local.hour * 60 + local.minute
    days = set(cfg["active_days"])
    if not overnight:
        return local.isoweekday() in days and start <= minute < end
    if local.isoweekday() in days and minute >= start:
        return True
    yesterday = (local - timedelta(days=1)).isoweekday()
    return yesterday in days and minute < end


def next_window_at(cfg, at):
    """UTC datetime of the next moment a window opens after `at`; None when always active or no day is allowed."""
    if not cfg["active_hours"]["enabled"]:
        return None
    local = at.astimezone(VN)
    start, _, _ = window(cfg)
    days = set(cfg["active_days"])
    for offset in range(0, 9):
        day = (local + timedelta(days=offset)).replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
        if day > local and day.isoweekday() in days:
            return day.astimezone(timezone.utc)
    return None


def clip(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def age(delta):
    minutes = max(0, int(delta.total_seconds() // 60))
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


class Heartbeat:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store

    # ---- settings -------------------------------------------------------------------------------------------
    def validate(self, value, current=None):
        """Merge a (partial) heartbeat payload onto `current` (or the defaults) and validate every field."""
        if not isinstance(value, dict):
            raise DomainError("Heartbeat settings must be an object.")
        unknown = set(value) - set(DEFAULTS)
        if unknown:
            raise DomainError("Unknown heartbeat setting: " + ", ".join(sorted(unknown)) + ".")
        base = current if current is not None else DEFAULTS
        result = {"active_hours": {**DEFAULTS["active_hours"], **(base.get("active_hours") or {})},
                  "pulse": {**DEFAULTS["pulse"], **(base.get("pulse") or {})}}
        for key in ("interval_seconds", "active_days", "pause_schedules_outside_hours"):
            result[key] = base.get(key, DEFAULTS[key])
        if "interval_seconds" in value:
            low, high = RANGES["interval_seconds"]
            if not _int(value["interval_seconds"]) or not low <= value["interval_seconds"] <= high:
                raise DomainError(f"The heartbeat interval must be a whole number of seconds from {low} to {high}.")
            result["interval_seconds"] = value["interval_seconds"]
        if "active_hours" in value:
            hours = value["active_hours"]
            if not isinstance(hours, dict) or set(hours) - set(DEFAULTS["active_hours"]):
                raise DomainError("Active hours accept enabled, start and end.")
            if "enabled" in hours and not isinstance(hours["enabled"], bool):
                raise DomainError("Active hours must be on or off.")
            for key in ("start", "end"):
                if key in hours and (not isinstance(hours[key], str) or not TIME.fullmatch(hours[key])):
                    raise DomainError("Active hours use the HH:MM format (GMT+7).")
            result["active_hours"].update(hours)
        if "active_days" in value:
            days = value["active_days"]
            if not isinstance(days, list) or not days or any(not _int(d) or not 1 <= d <= 7 for d in days):
                raise DomainError("Choose at least one active weekday (1 = Monday ... 7 = Sunday).")
            result["active_days"] = sorted(set(days))
        if "pause_schedules_outside_hours" in value:
            if not isinstance(value["pause_schedules_outside_hours"], bool):
                raise DomainError("Pausing scheduled work outside active hours must be on or off.")
            result["pause_schedules_outside_hours"] = value["pause_schedules_outside_hours"]
        if "pulse" in value:
            self._validate_pulse(value["pulse"], result["pulse"])
        return result

    def _validate_pulse(self, pulse, target):
        if not isinstance(pulse, dict) or set(pulse) - set(DEFAULTS["pulse"]):
            raise DomainError("Team pulse accepts enabled, agent_id, every_minutes, only_if_changed, notify and prompt.")
        for key in ("enabled", "only_if_changed"):
            if key in pulse and not isinstance(pulse[key], bool):
                raise DomainError("Team pulse switches must be on or off.")
        if "agent_id" in pulse:
            agent = self.store.get("agents", pulse["agent_id"]) if isinstance(pulse["agent_id"], str) else None
            if not agent or agent.get("archived"):
                raise DomainError("Choose an active agent for the team pulse.")
        if "every_minutes" in pulse:
            low, high = RANGES["every_minutes"]
            if not _int(pulse["every_minutes"]) or not low <= pulse["every_minutes"] <= high:
                raise DomainError(f"The team pulse interval must be from {low} minutes to {high} minutes (one day).")
        if "notify" in pulse and pulse["notify"] not in ("needed", "always"):
            raise DomainError("Team pulse notifications must be needed or always.")
        if "prompt" in pulse:
            text = pulse["prompt"]
            if not isinstance(text, str) or not text.strip() or len(text) > 4000:
                raise DomainError("The team pulse prompt must contain 1 to 4,000 characters.")
            require_english("The team pulse prompt", text)
            pulse = {**pulse, "prompt": text.strip()}
        target.update(pulse)

    def config(self):
        """The effective configuration; stored data that no longer validates falls back to defaults."""
        stored = self.store.settings().get("heartbeat") or {}
        try:
            return self.validate(stored, {})
        except DomainError:
            return self.validate({}, {})

    def interval(self):
        return self.config()["interval_seconds"]

    def state(self):
        return dict(self.store.settings().get("heartbeat_state") or {})

    def remember(self, **changes):
        self.store.settings({"heartbeat_state": {**self.state(), **changes}})

    def settings_changed(self, before, after):
        """Re-arm the pulse when it is switched on or its cadence changes."""
        old, new = (before or {}).get("pulse") or {}, (after or {}).get("pulse") or {}
        if new.get("enabled") and not old.get("enabled"):
            self.remember(next_pulse_at=None)
        elif new.get("every_minutes") != old.get("every_minutes") and self.state().get("last_pulse_at"):
            last = parse_time(self.state()["last_pulse_at"])
            self.remember(next_pulse_at=(last + timedelta(minutes=new.get("every_minutes", 60))).isoformat().replace("+00:00", "Z"))

    # ---- scheduled work ---------------------------------------------------------------------------------------
    def quiet(self, at, cfg=None):
        cfg = cfg or self.config()
        return cfg["pause_schedules_outside_hours"] and not active_at(cfg, at)

    def hold(self, schedule, at, cfg):
        """Quiet hours: park a due schedule at the next window start so it fires once when the window opens."""
        opening = next_window_at(cfg, at)
        if opening is None:
            return False
        self.store.patch("schedules", schedule["id"], {"next_run": opening.isoformat().replace("+00:00", "Z"), "held_at": now()})
        return True

    # ---- team pulse ---------------------------------------------------------------------------------------------
    def pulse_runs(self):
        return [r for r in self.store.all("runs") if r.get("system_pulse")]

    def digest(self, cfg, at, since=None):
        """Compact English digest of the team's state. No model call. Returns text, a stable hash and an `open` flag."""
        since = since or at - timedelta(hours=24)
        pulse = cfg["pulse"]
        stall_after = timedelta(minutes=max(10, 2 * pulse["every_minutes"]))
        agents = {a["id"]: a for a in self.store.all("agents")}
        name = lambda ident: (agents.get(ident) or {}).get("name") or ident or "unassigned"
        tasks = [t for t in self.store.all("tasks") if not t.get("archived") and not t.get("sample")]
        status = {t["id"]: t["status"] for t in tasks}

        def blocked(task):
            return task["status"] == "blocked" or (task["status"] in ("backlog", "ready") and any(
                status.get(dep) in ("failed", "cancelled") for dep in task.get("dependencies", [])))

        def recent(item):
            try:
                return at - parse_time(item.get("updated_at") or item.get("completed_at")) <= FAILED_WINDOW
            except (TypeError, ValueError):
                return False

        open_tasks = [t for t in tasks if t["status"] in OPEN_TASK and (t["status"] != "failed" or recent(t))]
        open_tasks += [t for t in tasks if blocked(t) and t not in open_tasks]
        blocked_ids = {t["id"] for t in open_tasks if blocked(t)}
        backlog = sum(1 for t in tasks if t["status"] == "backlog" and t not in open_tasks)
        runs = [r for r in self.store.all("runs") if not r.get("system_pulse") and r["status"] not in TERMINAL]
        counts = {s: sum(1 for r in runs if r["status"] == s) for s in ("running", "queued", "waiting_team", "waiting_quota", "retrying")}
        counts["waiting_owner"] = sum(1 for r in runs if r["status"] in OWNER_WAIT)
        stalled = []
        for run in runs:
            if run["status"] in ("running", "queued", "retrying"):
                try:
                    idle = at - parse_time(run.get("updated_at"))
                except (TypeError, ValueError):
                    continue
                if idle > stall_after:
                    stalled.append((run, idle))
        new_failures = []
        for run in self.store.all("runs"):
            if run["status"] == "failed" and not run.get("system_pulse"):
                try:
                    if parse_time(run.get("completed_at") or run["updated_at"]) > since:
                        new_failures.append(run)
                except (TypeError, ValueError):
                    pass
        decisions = [q for q in self.store.all("decisions") if q.get("status") == "open"]

        lines = ["Team pulse digest, " + at.astimezone(VN).strftime("%Y-%m-%d %H:%M") + " GMT+7"]
        by_status = {s: sum(1 for t in open_tasks if t["status"] == s) for s in OPEN_TASK}
        lines.append("Tasks: " + ", ".join(f"{n} {s}" for s, n in by_status.items() if n)
                     + (f"; {len(blocked_ids)} blocked" if blocked_ids else "")
                     + (f" ({backlog} more in backlog)" if backlog else "") if open_tasks else
                     "Tasks: none open" + (f" ({backlog} in backlog)" if backlog else ""))
        for task in open_tasks[:12]:
            detail = f" - {clip(task.get('error'), 100)}" if task["status"] == "failed" and task.get("error") else ""
            label = task["status"] + (", blocked" if task["id"] in blocked_ids and task["status"] != "blocked" else "")
            lines.append(f"- {task['id']} [{label}] {clip(task.get('title'), 80)} (owner {name(task.get('agent_id'))}){detail}")
        if len(open_tasks) > 12:
            lines.append(f"- ... and {len(open_tasks) - 12} more (use tasks.inspect)")
        lines.append("Runs: " + ", ".join(f"{n} {s.replace('_', ' ')}" for s, n in counts.items() if n) if any(counts.values()) else "Runs: none active")
        if stalled:
            lines.append(f"Stalled runs (no step for over {age(stall_after)}):")
            lines += [f"- {r['id']} {name(r['agent_id'])} [{r['status']}] idle {age(idle)}: {clip(r.get('prompt'), 70)}" for r, idle in stalled[:8]]
        if new_failures:
            lines.append("New failures since the last pulse:")
            lines += [f"- {r['id']} {name(r['agent_id'])}: {clip(r.get('error'), 110)}" for r in new_failures[:8]]
        lines.append(f"Pending owner decisions: {len(decisions)}")

        identity = {"tasks": sorted((t["id"], t["status"]) for t in open_tasks), "runs": sorted((r["id"], r["status"]) for r in runs),
                    "stalled": sorted(r["id"] for r, _ in stalled), "failures": sorted(r["id"] for r in new_failures),
                    "decisions": sorted(q["id"] for q in decisions)}
        digest_hash = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
        attention = bool(open_tasks or runs or decisions or new_failures)
        return {"text": "\n".join(lines), "hash": digest_hash, "open": attention}

    def pulse_tick(self, at):
        """Called from tick(): start a due pulse. Returns the outcome dict, or None when nothing was due."""
        cfg = self.config()
        pulse = cfg["pulse"]
        if not pulse["enabled"] or not active_at(cfg, at):
            return None
        due = self.state().get("next_pulse_at")
        if due and parse_time(due) > at:
            return None
        return self.run_pulse(at, cfg)

    def pulse_now(self):
        if not self.store.settings().get("execution_enabled", True):
            raise DomainError("Execution is paused. Enable execution before running a pulse.", 409)
        outcome = self.run_pulse(datetime.now(timezone.utc), self.config(), force=True)
        return {**outcome, "status": self.status()}

    def run_pulse(self, at, cfg, force=False):
        pulse = cfg["pulse"]
        stamp = at.isoformat().replace("+00:00", "Z")
        with self.engine.guard:
            if any(r["status"] not in TERMINAL for r in self.pulse_runs()):
                return {"started": False, "reason": "running"}  # never overlap; retried on the next heartbeat
            nxt = (at + timedelta(minutes=pulse["every_minutes"])).isoformat().replace("+00:00", "Z")
            state = self.state()

            def skip(reason, digest_hash=None):
                self.remember(last_pulse_at=stamp, last_result="skipped", last_reason=reason, next_pulse_at=nxt,
                              **({"last_hash": digest_hash} if digest_hash else {}))
                return {"started": False, "reason": reason}

            try:
                agent = self.store.get("agents", pulse["agent_id"])
            except DomainError:
                return skip("agent_unavailable")
            if agent.get("archived") or agent["status"] == "paused" or (not force and not agent.get("heartbeat")):
                return skip("agent_unavailable")
            since = parse_time(state["last_pulse_at"]) if state.get("last_pulse_at") else None
            digest = self.digest(cfg, at, since)
            if not digest["open"]:
                return skip("nothing_open", digest["hash"])
            if pulse["only_if_changed"] and not force and digest["hash"] == state.get("last_hash"):
                return skip("unchanged")
            mode = ("Notify mode: always. Finish with a short owner-facing status (what is moving, what is stuck)."
                    if pulse["notify"] == "always" else
                    "Notify mode: needed. Stay silent for the owner unless a decision or an important result needs them.")
            channel = "dm-" + agent["id"]
            run = self.engine.enqueue(agent["id"], pulse["prompt"].strip() + "\n\n" + mode + "\n\n" + digest["text"], "schedule",
                                      channel=channel, system_pulse=True, pulse_notify=pulse["notify"],
                                      owner_language=self.engine.channel_language(channel) or "en")
            self.remember(last_pulse_at=stamp, last_started_at=stamp, last_hash=digest["hash"], last_result="started",
                          last_reason=None, last_run_id=run["id"], next_pulse_at=nxt)
            return {"started": True, "run_id": run["id"], "reason": None}

    # ---- status -------------------------------------------------------------------------------------------------
    def status(self):
        cfg = self.config()
        at = datetime.now(timezone.utc)
        state = self.state()
        runs = self.store.all("runs")
        count = lambda *names: sum(1 for r in runs if r["status"] in names)
        result = state.get("last_result")
        if result == "started":
            try:
                result = {"done": "done", "failed": "failed", "cancelled": "cancelled"}.get(self.store.get("runs", state.get("last_run_id"))["status"], "running")
            except DomainError:
                result = "done"
        active = active_at(cfg, at)
        window_open = next_window_at(cfg, at) if not active else None
        pulse = cfg["pulse"]
        return {"last_heartbeat_at": self.engine.last_heartbeat, "server_time": now(), "interval_seconds": cfg["interval_seconds"],
                "execution_enabled": bool(self.store.settings().get("execution_enabled", True)),
                "active_now": active, "quiet_hours": not active,
                "next_window_at": window_open.isoformat().replace("+00:00", "Z") if window_open else None,
                "pulse": {"enabled": pulse["enabled"], "agent_id": pulse["agent_id"], "every_minutes": pulse["every_minutes"],
                          "last_pulse_at": state.get("last_pulse_at"), "last_result": result, "last_reason": state.get("last_reason"),
                          "next_pulse_at": state.get("next_pulse_at") if pulse["enabled"] else None},
                "queued": count("queued"), "running": count("running"),
                "waiting_owner": count(*OWNER_WAIT), "waiting_quota": count("waiting_quota")}
