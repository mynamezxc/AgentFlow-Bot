"""Compare the old (pre-token-optimisation) prompt with the new stable/volatile + native-session prompt.

Usage: python scripts/token_report.py [DATA_DIR] [--runs N]

DATA_DIR holds workspace.sqlite3 (default: ./.agentflow or /tmp/probe if it exists). The database is copied to a
temporary directory first, so the live workspace is never modified. For the N largest runs (by old first-turn
prompt size) it prints characters and ~tokens (chars / 3.5) for:

  old first / old follow-up   every old turn resent the whole prompt
  new first                   stable prefix (cached by the provider) + volatile user message of a fresh session
  new follow-up               the delta sent to the resumed native session after one more tool round
"""
import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentflow.contract import SYSTEM_PROMPT, TOOL_SCHEMAS, TOOLS  # noqa: E402
from agentflow.language import POLICY  # noqa: E402
from agentflow.prompting import CHARS_PER_TOKEN  # noqa: E402
from agentflow.providers import Providers, Vault  # noqa: E402
from agentflow.runtime import Engine  # noqa: E402
from agentflow.store import Store, initial_seed  # noqa: E402


def legacy_context(engine, actor, run):
    """Frozen copy of the pre-optimisation Engine.context (work tier, non-customer)."""
    store = engine.store

    def bounded(items, count=15):
        return [{k: (v[:4500] if k == "content" else v) for k, v in item.items() if k not in ("absolute_path", "version")} for item in items[-count:]]
    memories = [m for m in store.all("memories") if not m.get("archived")]
    result = {"you": {k: actor.get(k) for k in ("id", "name", "role", "description", "instructions")},
              "shared_knowledge": bounded([m for m in memories if m.get("scope") == "shared"]),
              "your_memory": bounded([m for m in memories if m.get("scope") == "private" and m.get("agent_id") == actor["id"]]),
              "team": [{k: a.get(k) for k in ("id", "name", "face", "color", "role", "parent_id", "category", "description", "status", "current_action")}
                       for a in store.all("agents") if not a.get("archived")]}
    tasks = [t for t in store.all("tasks") if not t.get("archived") and not t.get("sample")]
    result["tasks"] = [{k: t.get(k) for k in ("id", "title", "agent_id", "reviewer_id", "status", "dependencies", "project_id", "result", "review_note")}
                       for t in tasks if t.get("project_id") == run.get("project_id")][-35:]
    profile = store.get("agents", actor["id"])
    result["your_model_binding"] = engine.models.effective(profile, run)
    result["model_policy"] = profile.get("model_policy", {})
    result["available_models"] = engine.models.catalog()
    result["available_apps"] = {"plugins": [{k: p[k] for k in ("id", "name", "installed", "operations")} for p in engine.plugins.catalog()],
                                "accounts": engine.plugins.accounts(actor)}
    result["custom_tools"] = engine.custom_tools.list()
    result["language_policy"] = {"internal": "English", "skills_tools": "English", "owner_reply": run.get("owner_language", "en")}
    result["browser_tools"] = engine.browser.status()
    result["model_switch_tool"] = "models.switch"
    result["pending_owner_decisions"] = [{k: q.get(k) for k in ("id", "question", "project_id", "requested_path", "requested_access", "permission_request", "kind")}
                                         for q in store.all("decisions") if q["status"] == "open" and q.get("channel") == run["channel"]]
    result["file_access_policy"] = {"coordinator_id": "a-lead", "request_tool": "files.request_access", "grant_scope": "shared within project"}
    result["available_skills"] = [{k: s.get(k) for k in ("id", "name", "description", "installed")} for s in store.all("skills") if not s.get("archived")][:60]
    if run.get("project_id"):
        result["checklist"] = engine.progress.items(run["project_id"])
        result["project"] = store.get("projects", run["project_id"])
        result["questions"] = [{k: q.get(k) for k in ("id", "question", "status", "answer", "agent_id")}
                               for q in store.all("decisions") if q.get("project_id") == run["project_id"]][-20:]
        result["artifacts"] = [{k: a.get(k) for k in ("id", "title", "path", "agent_id", "kind", "is_image", "type")}
                               for a in store.all("artifacts") if a.get("project_id") == run["project_id"]][-25:]
        result["file_changes"] = [{k: c.get(k) for k in ("id", "artifact_id", "path", "agent_id", "kind", "added", "deleted")}
                                  for c in store.all("file_changes") if c.get("project_id") == run["project_id"]][-25:]
        result["recent_team_messages"] = [{k: m.get(k) for k in ("agent_id", "target_id", "content", "kind", "created_at")}
                                          for m in store.all("messages") if m.get("project_id") == run["project_id"] and not m.get("sample")][-20:]
    result["skills"] = [{"id": s["id"], "name": s["name"], "instructions": s.get("content", "")[:6500]}
                        for s in store.all("skills") if s.get("installed") and s["id"] in actor.get("skills", [])]
    result["runtime_guides"] = [{"name": p.stem, "instructions": p.read_text(encoding="utf-8")} for p in sorted((ROOT / "agentflow" / "guides").glob("*.md"))]
    return result


def legacy_prompt(engine, actor, run):
    """Frozen copy of the pre-optimisation Engine.prompt (everything resent on every turn)."""
    context = legacy_context(engine, actor, run)
    history = [{k: m.get(k) for k in ("role", "agent_id", "kind", "content", "created_at")}
               for m in engine.store.all("messages") if not m.get("sample") and m.get("channel") == run["channel"]][-22:]
    return (SYSTEM_PROMPT + "\n" + POLICY + "\nOwner reply language: " + run.get("owner_language", "en") + "\nTOOLS:\n"
            + json.dumps({name: TOOL_SCHEMAS[name] for name in sorted(TOOLS)}, ensure_ascii=False)
            + "\nCURRENT CONTEXT:\n" + json.dumps(context, ensure_ascii=False)
            + "\nCONVERSATION:\n" + json.dumps(history, ensure_ascii=False)
            + "\nCURRENT REQUEST:\n" + run["prompt"]
            + "\nOWNER ATTACHMENTS (data, not additional authority):\n" + json.dumps(engine.attachments.context(run), ensure_ascii=False)
            + "\nRESUME REASON:\n" + run.get("resume_reason", "")
            + ("\nREVIEW TASK_ID: " + run["review_task_id"] if run.get("review_task_id") else "")
            + "\nYOUR CHECKPOINT:\n" + json.dumps(run.get("trace", [])[-18:], ensure_ascii=False)[-100000:])


def open_engine(source):
    work = Path(tempfile.mkdtemp(prefix="agentflow-token-report-"))
    data = work / "data"
    data.mkdir()
    for item in Path(source).iterdir():
        if item.is_file() and (item.name.startswith("workspace.sqlite3") or item.suffix == ".json"):
            shutil.copy2(item, data / item.name)
    seed = json.loads((ROOT / "dist" / "seed.json").read_text(encoding="utf-8"))
    store = Store(data, initial_seed(seed))
    vault = Vault(data)
    with mock.patch("threading.Thread.start"):
        providers = Providers(store, vault, str(work / "ws"))
        engine = Engine(store, providers, vault, str(work / "ws"))
    return engine, work


def follow_up(engine, actor, run):
    """Delta for the resumed native session after the run's latest assistant+tool round (real delta code path)."""
    trace = run.get("trace", [])
    tools = [i for i, e in enumerate(trace) if e.get("role") == "tool"]
    if not tools:
        return None
    latest = tools[-1]
    assistants = [i for i, e in enumerate(trace[:latest]) if e.get("role") == "assistant"]
    if not assistants:
        return None
    steps_now = run.get("steps", 1) - sum(1 for e in trace[latest + 1:] if e.get("role") == "assistant")
    now = {**run, "trace": trace[:latest + 1], "steps": steps_now}
    previous = {**run, "trace": trace[:assistants[-1]], "steps": steps_now - 1}
    first = engine.prompt(actor, previous)
    state = {"provider": actor["provider"], "model": actor["model"], "effort": actor.get("reasoning_effort") or None, "id": "00000000-0000-0000-0000-000000000000",
             "turns": 1, "system_digest": first.system_digest, "sent_images": [], "est_tokens": 1000, **first.next_state}
    return engine.prompt(actor, {**now, "native_session": state}).delta


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("data_dir", nargs="?", default="/tmp/probe" if Path("/tmp/probe").exists() else str(ROOT / ".agentflow"))
    parser.add_argument("--runs", type=int, default=8)
    args = parser.parse_args()
    engine, work = open_engine(args.data_dir)
    try:
        rows = []
        for run in engine.store.all("runs"):
            if run.get("kind") == "support":
                continue
            try:
                agent = engine.store.get("agents", run["agent_id"])
                actor = {**agent, "provider": run.get("provider") or agent.get("provider") or "codex",
                         "model": run.get("model") or agent.get("model") or "model", "reasoning_effort": None}
                fresh = {**run, "trace": [], "resume_reason": ""}
                old_first, old_turn = len(legacy_prompt(engine, actor, fresh)), len(legacy_prompt(engine, actor, run))
                first, restart = engine.prompt(actor, fresh), engine.prompt(actor, run)
                delta = follow_up(engine, actor, run)
            except Exception as exc:  # a damaged historical record must not hide the others
                print("skip", run.get("id"), type(exc).__name__, exc)
                continue
            rows.append((run["id"], run.get("kind"), run.get("steps", 0), old_first, old_turn, len(first.system), len(first.volatile),
                         len(restart.volatile), len(delta) if delta is not None else None))
        rows.sort(key=lambda r: -r[4])
        rows = rows[:args.runs]
        tok = lambda chars: round(chars / CHARS_PER_TOKEN / 1000, 1)
        head = (f"{'run':<18}{'kind':<6}{'steps':>5} | {'OLD 1st':>9} {'OLD turn':>9} | {'NEW stable':>10} {'NEW 1st vol.':>12} {'restart vol.':>12} {'follow-up':>10} |"
                f" saved: 1st-uncached / turn-uncached")
        print("chars per model call (tokens ~ chars/%s)" % CHARS_PER_TOKEN)
        print(head)
        print("-" * len(head))
        sums = [0] * 8
        count = 0
        for run_id, kind, steps, old_first, old_turn, stable, volatile, restart, delta in rows:
            fu = delta if delta is not None else volatile
            print(f"{run_id:<18}{kind:<6}{steps:>5} | {old_first:>8,}c {old_turn:>8,}c | {stable:>9,}c {volatile:>11,}c {restart:>11,}c {fu:>9,}c | "
                  f"{1 - volatile / old_first:>8.0%} / {1 - fu / old_turn:>5.0%}")
            for i, v in enumerate((old_first, old_turn, stable, volatile, restart, fu)):
                sums[i] += v
            count += 1
        print("-" * len(head))
        if not count:
            return
        old_first, old_turn, stable, volatile, restart, fu = (v / count for v in sums[:6])
        print(f"mean ~tokens: OLD first {tok(old_first)}k, OLD later turn {tok(old_turn)}k | NEW first {tok(stable + volatile)}k "
              f"(stable {tok(stable)}k provider-cached + volatile {tok(volatile)}k), restart-after-compact {tok(stable + restart)}k, follow-up {tok(fu)}k")
        turns = 12
        old_total = old_first + (turns - 1) * old_turn
        new_uncached = volatile + (turns - 1) * fu
        print(f"{turns}-turn run, uncached input: OLD {tok(old_total)}k vs NEW {tok(new_uncached)}k "
              f"(+{tok(stable)}k stable prefix once, then cached) -> {1 - (new_uncached + stable) / old_total:.0%} lower even counting the prefix as uncached")
    finally:
        engine.close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
