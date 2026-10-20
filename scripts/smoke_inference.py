"""One isolated Codex inference to verify the CLI adapter. No external tool calls."""
import json
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from agentflow.providers import Providers, Vault
from agentflow.store import Store, now

os.environ.setdefault("CODEX_HOME", str(Path(os.getenv("USERPROFILE") or Path.home()) / ".codex"))
seed = json.loads((root / "dist" / "seed.json").read_text(encoding="utf-8"))
store = Store(root / ".agentflow", seed)
try:
    providers = Providers(store, Vault(store.directory), root / "workspaces" / "company")
    catalog = providers.models
    candidate = next((m for m in catalog if m["provider"] == "codex" and m["id"] == "gpt-6-luna"), None)
    if not candidate:
        raise SystemExit("Refresh the Codex catalog first. No model was inferred.")
    output = providers.infer({"name": "Connection test", "provider": "codex", "model": candidate["id"]},
                            'Return exactly this JSON and do not call any tool: {"reply":"AgentFlow connection verified"}',
                            "connection-smoke-test")
    result = {"provider": "codex", "model": candidate["id"], "verified_at": now(), "result": output}
    (store.directory / "connection-probe.json").write_text(json.dumps(result), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))
finally:
    store.close()
