"""Read persisted live QA evidence; no inference or external side effects."""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument("--run-id")
args=parser.parse_args()
run_id=args.run_id or json.loads((ROOT/".agentflow/live-capabilities-ids.json").read_text())["run_id"]
conn=sqlite3.connect((ROOT/".agentflow/workspace.sqlite3").as_uri()+"?mode=ro",uri=True)
def records(kind):
    return [json.loads(p) for p, in conn.execute("SELECT payload FROM records WHERE kind=? ORDER BY rowid",(kind,))]
try:
    runs=[r for r in records("runs") if r.get("root_id")==run_id]
    root=next(r for r in runs if r["id"]==run_id)
    assert root["status"]=="done", "Root QA is not complete"
    agents={a["id"]:a for a in records("agents")}
    chain=[];parent=root["agent_id"]
    for _ in range(3):
        child=next(a for a in agents.values() if a.get("created_by")==parent and a.get("parent_id")==parent)
        chain.append({k:child.get(k) for k in ("id","name","role","parent_id","skills")});parent=child["id"]
    assert chain[-1]["role"]=="Trình bày thông tin QA" and chain[-1]["skills"], "Role/skills were not rebuilt"
    events=[e for e in records("tool_events") if e.get("run_id")==run_id and e.get("status")=="done"]
    required={"skills.create","tools.create","tools.invoke","agents.update","agents.inspect"}
    assert required <= {e["name"] for e in events}, "Required broker operations missing"
    artifact=ROOT/"workspaces/company/qa-capabilities/evidence.md"
    assert "AGENTFLOW_DYNAMIC_CAPABILITIES_OK" in artifact.read_text(encoding="utf-8")
    assert any(m.get("reasoning_effort")=="medium" for m in root["model_history"]), "No real reasoning-selected inference"
    proof={"root_id":run_id,"status":root["status"],"chain":chain,"runs":[{"id":r["id"],"status":r["status"],"steps":r.get("steps")} for r in runs],
           "model_history":root["model_history"],"tool_names":sorted({e["name"] for e in events}),
           "artifact_sha256":hashlib.sha256(artifact.read_bytes()).hexdigest()}
    (ROOT/".agentflow/capabilities-proof.json").write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"ok":True,"runs":len(runs),"model_turns":sum(r.get("steps",0) for r in runs),"hierarchy_depth":len(chain),"proof":".agentflow/capabilities-proof.json"}))
finally:
    conn.close()
