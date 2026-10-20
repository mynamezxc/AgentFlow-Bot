"""Verify persisted live evidence, without calling a model or mutating work."""
import hashlib
import json
import sqlite3
from pathlib import Path

root = Path(__file__).resolve().parents[1]
connection = sqlite3.connect(root / ".agentflow" / "workspace.sqlite3")
def records(kind):
    return [json.loads(row[0]) for row in connection.execute(
        "SELECT payload FROM records WHERE kind=? ORDER BY rowid", (kind,))]

runs = records("runs")
primary = [r for r in runs if r.get("kind") == "chat" and r.get("steps", 0) > 1
           and "qa-launch/summary.md" in r.get("prompt", "")][-1]
project_id = primary["project_id"]
team_runs = [r for r in runs if r.get("root_id") == primary["root_id"]]
run_ids = {r["id"] for r in team_runs}
tasks = [t for t in records("tasks") if t.get("root_id") == primary["root_id"]]
questions = [q for q in records("decisions") if q.get("root_id") == primary["root_id"]]
tools = [t for t in records("tool_events") if t.get("run_id") in run_ids]
artifacts = [a for a in records("artifacts") if a.get("run_id") in run_ids]
assert primary["status"] == "done", "Coordinator incomplete"
assert all(r["status"] == "done" for r in team_runs), "Unfinished branch"
assert tasks and all(t["status"] == "done" and t.get("score")
                     and t["agent_id"] != t["reviewer_id"] for t in tasks)
assert questions and all(q["status"] == "answered" for q in questions)
assert any(t["name"] == "agents.create" and t["status"] == "done" for t in tools)
assert any(t["name"] == "skills.create" and t["status"] == "done" for t in tools)
assert any(t["name"] == "tasks.review" and t["status"] == "done" for t in tools)
files = []
for artifact in artifacts:
    path = Path(artifact["absolute_path"]).resolve()
    assert path.is_relative_to(root / "workspaces" / "company") and path.is_file()
    files.append({"path": artifact["path"], "bytes": path.stat().st_size,
                  "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
assert {"brief.md", "summary.md"}.issubset({Path(f["path"]).name for f in files})
proof = {"root_id": primary["root_id"], "project_id": project_id,
         "runs": [{"id": r["id"], "agent_id": r["agent_id"], "status": r["status"],
                   "steps": r["steps"], "provider": r.get("provider"), "model": r.get("model")} for r in team_runs],
         "task_scores": [t["score"] for t in tasks], "decisions_answered": len(questions),
         "successful_tool_calls": sum(t["status"] == "done" for t in tools),
         "artifacts": files, "external_accounts_exercised": ["Codex CLI"]}
(root / ".agentflow" / "collaboration-proof.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
print(json.dumps(proof, indent=2))
connection.close()
