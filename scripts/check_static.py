import json
import re
from pathlib import Path

root = Path(__file__).resolve().parents[1]
js = (root / "dist" / "app.js").read_text(encoding="utf-8")
css = (root / "dist" / "styles.css").read_text(encoding="utf-8")
html = (root / "dist" / "index.html").read_text(encoding="utf-8")
design = (root / "DESIGN.md").read_text(encoding="utf-8")
issues = []
for token, value in re.findall(r'^  ([a-z-]+): "(#[a-fA-F0-9]{6})"', design, re.M):
    if not re.search(r"--" + token + r"\s*:\s*" + re.escape(value), css, re.I):
        issues.append("Token drift: " + token)
if re.search(r"\b(?:alert|confirm|prompt)\s*\(", js):
    issues.append("Native dialog call")
if 'href="#"' in js or 'href="#"' in html:
    issues.append("Dead anchor")
if 'lang="en"' not in html:
    issues.append("Missing English document language")
for snippet in ["scrollbar-width:", "scrollbar-color:", "prefers-reduced-motion", "forced-colors", "focus-visible", "resize:none"]:
    if snippet not in css:
        issues.append("Missing stylesheet contract: " + snippet)
for snippet in ["novalidate", 'aria-invalid', 'aria-describedby', 'aria-labelledby']:
    if snippet not in js + html:
        issues.append("Missing accessibility contract: " + snippet)
seed = json.loads((root / "dist" / "seed.json").read_text(encoding="utf-8"))
assert not any(a.get("model") for a in seed["agents"]), "Model IDs must be discovered, not fabricated"
print(json.dumps({"ok": not issues, "issues": issues, "checks": 18}, ensure_ascii=False, indent=2))
raise SystemExit(bool(issues))
