"""Read a provider model catalog. Does not start inference or print credentials."""
import json
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from agentflow.providers import Providers, Vault
from agentflow.store import Store

os.environ.setdefault("CODEX_HOME", str(Path(os.getenv("USERPROFILE") or Path.home()) / ".codex"))
seed = json.loads((root / "dist" / "seed.json").read_text(encoding="utf-8"))
store = Store(root / ".agentflow", seed)
try:
    providers = Providers(store, Vault(store.directory), root / "workspaces" / "company")
    models = providers.refresh_models("codex")
    print(json.dumps({"source": "Codex app-server model/list", "models": models}, ensure_ascii=True, indent=2))
finally:
    store.close()
