import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .store import DomainError
from .research import public_page
from .language import english_authoring
from .authoring import optimize_skill


def skill_document(data):
    document = dict(data)
    if document.get("url"):
        url = str(document["url"])
        parsed = urlparse(url)
        if parsed.hostname == "github.com" and "/blob/" in parsed.path:
            url = "https://raw.githubusercontent.com" + parsed.path.replace("/blob/", "/", 1)
            parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "raw.githubusercontent.com" or not parsed.path.endswith("SKILL.md"):
            raise DomainError("Provide a GitHub SKILL.md URL or Markdown content.")
        try:
            page = public_page(url, max_chars=100001)
            if page["status"] != 200 or urlparse(page["url"]).hostname != "raw.githubusercontent.com":
                raise DomainError("Cannot read SKILL.md from GitHub.")
            if len(page["text"].encode()) > 100000:
                raise DomainError("SKILL.md exceeds 100 KB.")
            document["content"] = page["text"]
            document["source_url"] = url
            document.setdefault("source", "GitHub")
        except (OSError, UnicodeError):
            raise DomainError("Cannot download SKILL.md. Check the URL or create your own instructions.", 502) from None
    content = str(document.get("content", "")).strip()
    if not content or len(content.encode()) > 100000:
        raise DomainError("Skills require Markdown content between 1 byte and 100 KB.")
    name = re.search(r"(?m)^name:\s*[\"']?([^\"'\n]+)", content[:5000])
    title = re.search(r"(?m)^#\s+(.+)", content)
    document["name"] = str(document.get("name") or (name.group(1) if name else title.group(1) if title else "Custom skill"))[:250]
    document["content"] = content
    document["sha256"] = hashlib.sha256(content.encode()).hexdigest()
    document.setdefault("description", "Reusable operating guidance authored by the team.")
    document.setdefault("category", "custom")
    document.setdefault("source", "Team")
    document["installed"] = True
    return {key: value for key, value in document.items() if key in {
        "name", "content", "description", "category", "source", "source_url", "sha256", "installed"}}


class SkillLibrary:
    def __init__(self, store, workspace):
        self.store, self.workspace = store, Path(workspace)

    def save(self, data, agent_id, skill_id=None, preserve=False):
        doc = skill_document(data)
        advisories = []
        # Preserve third-party source documents; agent-authored methods are English and optimized.
        # `preserve`: owner-selected local/third-party imports keep their content exactly as written.
        if not preserve and not data.get('url') and not data.get('id'):
            doc, advisories = optimize_skill(doc, self.store.all("skills"), skill_id)
            doc["sha256"] = hashlib.sha256(doc["content"].encode()).hexdigest()
        self.store.get("agents", agent_id)
        with self.store.transaction():
            if skill_id:
                previous = self.store.get("skills", skill_id)
                self.store.create("skill_versions", {"skill_id": skill_id, "snapshot": previous, "agent_id": agent_id})
                item = self.store.patch("skills", skill_id, {**doc, "authored_by": agent_id})
            else:
                existing = next((s for s in self.store.all("skills") if s.get("sha256") == doc["sha256"]), None)
                item = self.store.patch("skills", existing["id"], {"installed": True}) if existing else self.store.create("skills", {**doc, "authored_by": agent_id})
            owner = self.store.get("agents", agent_id)
            self.store.patch("agents", agent_id, {"skills": list(dict.fromkeys(owner.get("skills", []) + [item["id"]]))})
            folder = self.workspace / "skills" / re.sub(r"[^a-zA-Z0-9_-]", "-", item["id"])
            folder.mkdir(parents=True, exist_ok=True)
            temporary = folder / "SKILL.md.tmp"
            temporary.write_text(item["content"], encoding="utf-8")
            temporary.replace(folder / "SKILL.md")
            return {"id": item["id"], "name": item["name"], "version": item["version"], "installed": True, "agent_id": agent_id,
                    **({"advisories": advisories} if advisories else {})}

    def install(self, data, agent_id):
        if data.get("skill_id"):
            item = self.store.get("skills", data["skill_id"])
            return self.save(item, agent_id, item["id"])
        return self.save(data, agent_id)

    def search(self, query):
        words = str(query).casefold().split()
        results = [s for s in self.store.all("skills") if not s.get("archived")]
        results.sort(key=lambda s: sum(w in (s["name"] + " " + s.get("description", "")).casefold() for w in words), reverse=True)
        return {"skills": [{k: s.get(k) for k in ("id", "name", "description", "source", "source_url", "installed", "version")} for s in results[:25]],
                "sources": [{"name": "Claude", "url": "https://github.com/anthropics/skills/tree/main/skills"},
                            {"name": "Codex", "url": "https://github.com/openai/skills/tree/main/skills"},
                            {"name": "Google Workspace", "url": "https://github.com/googleworkspace/cli/tree/main/skills"},
                            {"name": "AgenticSkills", "url": "https://agenticskills.io/"}]}

    def research(self, query, url=None):
        if url:
            parsed = urlparse(str(url))
            if parsed.hostname == "github.com" and "/blob/" in parsed.path:
                url = "https://raw.githubusercontent.com" + parsed.path.replace("/blob/", "/", 1)
            page = public_page(url, max_chars=100000)
            return {"source":page, "next_step":"Read and verify the source. Install an English SKILL.md with skills.install, or author professional English guidance with skills.create and cite the source."}
        sources = [("Anthropic", "anthropics/skills", "skills"),
                   ("OpenAI", "openai/skills", "skills/.curated"),
                   ("Google Workspace", "googleworkspace/cli", "skills")]
        words = str(query).casefold().split()
        candidates, errors = [], {}
        for name, repo, folder in sources:
            try:
                page = public_page("https://api.github.com/repos/"+repo+"/contents/"+folder, max_chars=300000)
                if page["status"] != 200:
                    raise DomainError("Catalog unavailable: HTTP "+str(page["status"]))
                entries = json.loads(page["text"])
                if not isinstance(entries, list):
                    raise DomainError("The catalog is not a directory list.")
                for entry in entries:
                    if entry.get("type") != "dir":
                        continue
                    path = entry["path"]
                    candidates.append({"name":entry["name"], "source":name,
                        "url":"https://raw.githubusercontent.com/"+repo+"/main/"+path+"/SKILL.md",
                        "score":sum(w in entry["name"].casefold() for w in words)})
            except (DomainError, ValueError, KeyError):
                errors[name] = "Chưa tải được catalog; có thể đọc link chính thức qua web.fetch."
        candidates.sort(key=lambda item:item["score"], reverse=True)
        return {"local":self.search(query), "candidates":candidates[:40], "errors":errors,
                "next_step":"Read and verify suitable English SKILL.md sources, then install them. Author reusable English instructions for missing capabilities. Never run downloaded host scripts; compose broker tools if needed."}

    def scan(self, roots=None):
        home = Path(os.getenv("USERPROFILE") or Path.home())
        roots = roots or [(home / ".agents" / "skills", "Open skills local"),
            (home / ".codex" / "skills", "Codex local"),
            (home / ".codex" / "skills" / ".system", "Codex system"),
            (home / ".claude" / "skills", "Claude local"),
            (self.workspace / "skills", "Company")]
        known = {s.get("sha256") for s in self.store.all("skills")}
        added = []
        for root, source in roots:
            if not root.is_dir() or root.is_symlink():
                continue
            for path in list(root.glob("*/SKILL.md"))[:250]:
                if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > 100000:
                    continue
                doc = skill_document({"content": path.read_text(encoding="utf-8"), "source": source})
                if doc["sha256"] in known:
                    continue
                item = self.store.create("skills", {**doc, "installed": False})
                known.add(doc["sha256"]);added.append({"id": item["id"], "name": item["name"]})
        return {"imported": len(added), "skills": added}
