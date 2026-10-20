"""Registry and cleanup for native CLI sessions that AgentFlow itself created.

Native sessions (Claude `--session-id/--resume`, Codex `exec resume`) let a run send only the
delta each turn. The CLIs persist them under their own home directories, so AgentFlow keeps a
small registry and deletes only registered files once the owning run is finished.
"""
import json
import re
import threading
import time
from pathlib import Path

SESSION_ID = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{7,79}$")


class UsageSemantics(dict):
    """provider -> 'cumulative' | 'per_turn' for the usage a CLI reports on resumed sessions (persisted as json).

    `codex exec --json` reports thread-cumulative usage by default; if a CLI version turns out to report per turn,
    providers set `usage_semantics['codex'] = 'per_turn'` and AgentFlow stops subtracting."""
    def __init__(self, directory):
        super().__init__()
        self.path = Path(directory) / "usage-semantics.json"
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.update({k: v for k, v in data.items() if v in ("cumulative", "per_turn")} if isinstance(data, dict) else {})
        except (OSError, ValueError):
            pass

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        try:
            self.path.write_text(json.dumps(dict(self)), encoding="utf-8")
        except OSError:
            pass

    def mode(self, provider):
        return self.get(provider, "cumulative")


COMPONENTS = ("input_tokens", "cached_input_tokens", "output_tokens")


def codex_turn(raw, previous, prior_turns, semantics):
    """Per-turn Codex usage from the numbers `turn.completed.usage` reported.

    raw / previous: {input_tokens (INCLUDES cached), cached_input_tokens, output_tokens}; previous is the last reported
    value of the same thread (None for a fresh thread). Returns (per_turn_raw, seen_raw). Cumulative semantics:
    per-turn = raw - previous when every component is >= previous, otherwise the thread is treated as fresh.
    Guard: on the first resumed turn a reported input below the previous turn's means the CLI reports per turn;
    `semantics['codex']` is switched to 'per_turn' (persisted) and nothing is subtracted from then on."""
    raw = {k: int(raw.get(k, 0) or 0) for k in COMPONENTS}
    if previous is None or semantics.mode("codex") == "per_turn":
        return raw, raw
    previous = {k: int(previous.get(k, 0) or 0) for k in COMPONENTS}
    if prior_turns == 1 and raw["input_tokens"] < previous["input_tokens"]:
        semantics["codex"] = "per_turn"
        return raw, raw
    if all(raw[k] >= previous[k] for k in COMPONENTS):
        return {k: raw[k] - previous[k] for k in COMPONENTS}, raw
    return raw, raw


MIGRATION_KEY = "codex_cumulative_usage_v1"
USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "cache_creation_tokens", "output_tokens")


def estimate_cumulative_total(summed, turns, segments=1):
    """Lower-bound estimate of a thread's true total from the SUM of its per-turn cumulative reports.

    Reports C_1 <= ... <= C_n are running totals, so the old accounting summed them (S = C_1+...+C_n). When the context
    grows from turn to turn, C is convex with C_0 = 0 and therefore C_k <= (k/n)*C_n, i.e. S <= C_n*(n+1)/2 and
    C_n >= 2*S/(n+1). The estimate 2*S/(n+1) is exact for steady growth and never above the truth for growing contexts,
    so it is a conservative (low) figure, not a measurement. A run that restarted its thread `segments - 1` times is
    treated as `segments` equal threads of n/segments turns each."""
    turns = max(1, int(turns))
    segments = min(max(1, int(segments)), turns)
    per_thread = turns / segments
    return int(2 * summed / (per_thread + 1)) if per_thread > 1 else int(summed)


def migrate_codex_usage(store):
    """One-time: flag historical Codex runs whose token usage was summed from cumulative reports.

    Only runs that used a persisted native session with >= 2 Codex turns are affected (earlier runs were one
    ephemeral thread per turn and are accurate). For those runs `token_usage` is replaced by the lower-bound estimate
    above, the raw numbers move to `token_usage_raw`, and `token_usage_overcounted: true` is set. Root `family_tokens`
    (and `family_cached_tokens`) and the agent `token_totals` are reduced by the difference, raw values kept in
    `family_tokens_raw` / `token_totals_raw`. Idempotent; marked in <store>/token-accounting.json."""
    path = Path(store.directory) / "token-accounting.json"
    try:
        marks = json.loads(path.read_text(encoding="utf-8"))
        marks = marks if isinstance(marks, dict) else {}
    except (OSError, ValueError):
        marks = {}
    if marks.get(MIGRATION_KEY):
        return {}
    roots, agents, changed = {}, {}, 0
    with store.transaction():
        for run in store.all("runs"):
            usage = run.get("token_usage")
            session = run.get("native_session")
            if not isinstance(usage, dict) or "token_usage_overcounted" in run or not session:
                continue
            history = run.get("model_history") or []
            codex = sum(1 for h in history if h.get("provider") == "codex") if history else (1 if run.get("provider") == "codex" else 0)
            share = codex / len(history) if history else float(bool(codex))
            turns = round(int(usage.get("turns", 0) or 0) * share)
            if share <= 0 or turns < 2:
                continue
            segments = 1 + int(session.get("resets", 0) or 0)
            new = dict(usage)
            for field in USAGE_FIELDS:
                part = int(usage.get(field, 0) or 0) * share
                new[field] = int(usage.get(field, 0) or 0) - int(part) + estimate_cumulative_total(part, turns, segments)
            lost = {field: int(usage.get(field, 0) or 0) - new[field] for field in USAGE_FIELDS}
            store.patch("runs", run["id"], {"token_usage": new, "token_usage_raw": usage, "token_usage_overcounted": True})
            changed += 1
            billed = sum(lost[f] for f in ("input_tokens", "cache_creation_tokens", "output_tokens"))
            root = roots.setdefault(run.get("root_id") or run["id"], {"tokens": 0, "cached": 0})
            root["tokens"] += billed
            root["cached"] += lost["cached_input_tokens"]
            agent = agents.setdefault(run.get("agent_id"), {f: 0 for f in USAGE_FIELDS})
            for field in USAGE_FIELDS:
                agent[field] += lost[field]
        for root_id, lost in roots.items():
            try:
                root = store.get("runs", root_id)
            except Exception:
                continue
            total, cached = int(root.get("family_tokens", 0) or 0), int(root.get("family_cached_tokens", 0) or 0)
            reduced = max(0, total - lost["tokens"])
            offset = int(root.get("family_tokens_offset", 0) or 0)
            store.patch("runs", root_id, {"family_tokens": reduced, "family_tokens_raw": root.get("family_tokens_raw", total),
                                          "family_cached_tokens": max(0, cached - lost["cached"]), "family_tokens_estimated": True,
                                          **({"family_tokens_offset": min(offset, reduced)} if offset else {})})
        for agent_id, lost in agents.items():
            try:
                agent = store.get("agents", agent_id)
            except Exception:
                continue
            totals = agent.get("token_totals") or {}
            store.patch("agents", agent_id, {"token_totals_raw": agent.get("token_totals_raw", totals),
                                             "token_totals": {**totals, **{f: max(0, int(totals.get(f, 0) or 0) - lost[f]) for f in USAGE_FIELDS}}})
    marks[MIGRATION_KEY] = {"runs": changed}
    try:
        path.write_text(json.dumps(marks), encoding="utf-8")
    except OSError:
        pass
    return {"runs": changed, "roots": len(roots)}


class NativeSessions:
    def __init__(self, providers):
        self.providers = providers
        self.path = Path(providers.store.directory) / "native-sessions.json"
        self.lock = threading.RLock()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [e for e in data if isinstance(e, dict) and e.get("session_id")] if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save(self, entries):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def entries(self):
        with self.lock:
            return self._load()

    def register(self, run_id, provider, session_id):
        if provider not in ("codex", "claude", "gemini") or not isinstance(session_id, str) or not SESSION_ID.match(session_id):
            return False
        with self.lock:
            entries = self._load()
            if any(e["session_id"] == session_id and e.get("provider") == provider for e in entries):
                return True
            entries.append({"session_id": session_id, "provider": provider, "run_id": run_id, "created": time.time()})
            try:
                self._save(entries[-2000:])
            except OSError:
                return False
        return True

    def files(self, provider, session_id):
        """Concrete session files for one registered id; never matches anything else."""
        if not SESSION_ID.match(session_id or ""):
            return []
        try:
            home = Path(self.providers.accounts.home(provider))
            if provider == "claude":
                return [p for p in (home / "projects").glob("*/" + session_id + ".jsonl") if p.is_file()]
            if provider == "codex":
                return [p for p in (home / "sessions").glob("**/rollout-*" + session_id + "*.jsonl") if p.is_file()]
            if provider == "gemini":
                # Gemini CLI keeps chats at <home>/tmp/<project>/chats/session-<timestamp>-<first 8 chars of the id>.json[l]
                return [p for p in (home / "tmp").glob("*/chats/session-*" + session_id[:8] + ".json*") if p.is_file()]
        except (OSError, KeyError):
            pass
        return []

    def prune(self, keep_run_ids=(), older_than_s=3600):
        keep = set(keep_run_ids or ())
        freed, count = 0, 0
        with self.lock:
            remaining = []
            for entry in self._load():
                old = time.time() - float(entry.get("created", 0)) >= older_than_s
                if entry.get("run_id") in keep or not old:
                    remaining.append(entry)
                    continue
                for file in self.files(entry.get("provider"), entry["session_id"]):
                    try:
                        size = file.stat().st_size
                        file.unlink()
                        freed += size
                        count += 1
                    except OSError:
                        continue
            try:
                self._save(remaining)
            except OSError:
                pass
        return {"freed_bytes": freed, "files": count}
