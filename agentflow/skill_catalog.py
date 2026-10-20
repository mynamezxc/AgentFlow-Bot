"""Skill catalog for the /install-skill picker: installed, built-in library, local Claude/Codex skills and
owner-configured third-party GitHub sources. Listing only reads SKILL.md front matter (never executes anything).

Refs (contract 5):  installed:<id>  library:<id>  claude:<path relative to the Claude home>
                    codex:<path relative to the Codex home>  third_party:<source_id>:<path in the repo>
"""
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlparse

from .research import public_page
from .skills import skill_document
from .store import DomainError

MAX_ITEMS = 500
MAX_SOURCES = 10
LOCAL_TTL = 10
REMOTE_TTL = 3600
HEAD_BYTES = 4096
MAX_DEPTH = 7
PRUNE = {"node_modules", ".git", "__pycache__", ".venv", "venv", "cache", "dist", "build"}
LOCAL_SOURCES = (("claude", "Claude local"), ("codex", "Codex local"))
KIND_LABELS = (("installed", "Installed"), ("library", "Library"), ("claude", "Claude"), ("codex", "Codex"),
               ("third_party", "Third-party"))
SKIP_DIRS = {"docs", "doc", "examples", "example", "tests", "test", "scripts", "assets", "node_modules", "template", "templates"}
GITHUB_HOSTS = ("github.com", "raw.githubusercontent.com")


def parse_front(text):
    """(name, description) from SKILL.md front matter; tolerant of quotes and folded scalars."""
    text = str(text or "")[:HEAD_BYTES].lstrip("﻿")
    match = re.match(r"---\s*\r?\n(.*?)\r?\n---", text, re.S)
    meta, key = {}, None
    if match:
        for line in match.group(1).splitlines():
            if re.match(r"\s", line) and key:
                meta[key] = (meta[key] + " " + line.strip()).strip()
                continue
            name, sep, value = line.partition(":")
            if sep and re.fullmatch(r"[A-Za-z_-]+", name.strip()):
                key = name.strip().lower()
                value = value.strip()
                meta[key] = "" if value in (">", "|", ">-", "|-") else value.strip("\"'")
    return meta.get("name", "").strip()[:120], " ".join(meta.get("description", "").split())[:300]


def slug(text, fallback="source"):
    return re.sub(r"[^a-z0-9]+", "-", str(text).casefold()).strip("-")[:40] or fallback


def parse_github(url):
    """-> dict(owner, repo, branch, path, file) for a GitHub repo/tree/blob/raw URL; None when unsupported."""
    parsed = urlparse(str(url))
    if parsed.scheme != "https" or parsed.hostname not in GITHUB_HOSTS or parsed.username or parsed.password or parsed.port:
        return None
    parts = [unquote(p) for p in parsed.path.split("/") if p]
    if any(p in (".", "..") for p in parts):
        return None
    if parsed.hostname == "raw.githubusercontent.com":
        if len(parts) < 4 or parts[-1] != "SKILL.md":
            return None
        return {"owner": parts[0], "repo": parts[1], "branch": parts[2], "path": "/".join(parts[3:]), "file": True}
    if len(parts) < 2:
        return None
    result = {"owner": parts[0], "repo": parts[1].removesuffix(".git"), "branch": None, "path": "", "file": False}
    if len(parts) >= 4 and parts[2] in ("tree", "blob"):
        result.update(branch=parts[3], path="/".join(parts[4:]), file=parts[2] == "blob")
    elif len(parts) != 2:
        return None
    if result["file"] and not result["path"].endswith("SKILL.md"):
        return None
    if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", result[k]) for k in ("owner", "repo")):
        return None
    return result


def normalize_sources(value):
    """settings.skill_sources: list (<= 10) of {id,label,url}; https GitHub repo/tree/SKILL.md URLs only."""
    if not isinstance(value, list) or len(value) > MAX_SOURCES:
        raise DomainError("Skill sources must be a list of at most %d entries." % MAX_SOURCES)
    result, seen = [], set()
    for item in value:
        if not isinstance(item, dict):
            raise DomainError("Each skill source needs a label and a GitHub URL.")
        label = " ".join(str(item.get("label") or "").split())
        url = str(item.get("url") or "").strip()
        if not 1 <= len(label) <= 60:
            raise DomainError("Skill source labels must contain 1 to 60 characters.")
        if len(url) > 300 or not parse_github(url):
            raise DomainError("Skill source URLs must be https GitHub repository, folder or SKILL.md links.")
        sid = str(item.get("id") or "").strip()
        if sid and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", sid):
            raise DomainError("Skill source IDs use lowercase letters, digits, - and _.")
        sid = sid or slug(label)
        base, n = sid, 2
        if item.get("id") and sid in seen:
            raise DomainError("Skill source IDs must be unique.")
        while sid in seen:
            sid, n = (base[:36] + "-" + str(n)), n + 1
        seen.add(sid)
        result.append({"id": sid, "label": label, "url": url})
    return result


class SkillCatalog:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self.lock = threading.RLock()
        self.local_cache = {}   # (kind, home) -> (time, items)
        self.remote_cache = {}  # (source_id, url) -> (time, items, error)

    # ---- helpers -------------------------------------------------------------------------
    def invalidate(self):
        with self.lock:
            self.local_cache.clear()
            self.remote_cache.clear()

    def home(self, kind):
        return Path(self.engine.providers.accounts.home(kind))

    def sources(self):
        value = self.store.settings().get("skill_sources") or []
        try:
            return normalize_sources(value)
        except DomainError:
            return []

    def installed_by_name(self):
        return {str(s.get("name", "")).casefold(): s for s in self.store.all("skills") if s.get("installed") and not s.get("archived")}

    # ---- local scan ----------------------------------------------------------------------
    def walk(self, home, bases):
        """SKILL.md files below `bases` (relative to home): bounded depth, no symlinks, pruned noise."""
        found = []
        for base in bases:
            root = home / base
            if not root.is_dir() or root.is_symlink():
                continue
            for folder, dirs, files in os.walk(root, followlinks=False):
                depth = len(Path(folder).relative_to(root).parts)
                dirs[:] = sorted(d for d in dirs if d not in PRUNE and not (Path(folder) / d).is_symlink()) if depth < MAX_DEPTH else []
                if "SKILL.md" in files:
                    path = Path(folder) / "SKILL.md"
                    if not path.is_symlink():
                        found.append(path)
                if len(found) >= MAX_ITEMS:
                    return found
        return found

    def scan_local(self, kind):
        home = self.home(kind)
        key = (kind, str(home))
        with self.lock:
            cached = self.local_cache.get(key)
            if cached and time.monotonic() - cached[0] < LOCAL_TTL:
                return cached[1]
        bases = ("skills", "plugins") if kind == "claude" else ("skills",)
        items = []
        for path in self.walk(home, bases):
            try:
                with open(path, "rb") as handle:
                    head = handle.read(HEAD_BYTES).decode("utf-8", errors="replace")
            except OSError:
                continue
            name, description = parse_front(head)
            relative = path.relative_to(home).as_posix()
            origin = path.parent.parent.relative_to(home).as_posix() if path.parent.parent != home else ""
            items.append({"ref": kind + ":" + relative, "name": name or path.parent.name, "description": description,
                          "source": kind, "origin": "~/" + home.name + "/" + origin if origin else "~/" + home.name})
        items.sort(key=lambda i: (i["name"].casefold(), i["ref"]))
        items = items[:MAX_ITEMS]
        with self.lock:
            self.local_cache[key] = (time.monotonic(), items)
        return items

    # ---- third party ---------------------------------------------------------------------
    @staticmethod
    def raw_url(info, branch, path):
        return "https://raw.githubusercontent.com/%s/%s/%s/%s" % (
            quote(info["owner"]), quote(info["repo"]), quote(branch), "/".join(quote(p) for p in path.split("/")))

    @staticmethod
    def api_list(info, path):
        url = "https://api.github.com/repos/%s/%s/contents/%s" % (quote(info["owner"]), quote(info["repo"]), "/".join(quote(p) for p in path.split("/") if p))
        return url.rstrip("/") + ("?ref=" + quote(info["branch"]) if info["branch"] else "")

    def list_dir(self, info, path):
        page = public_page(self.api_list(info, path), max_chars=300000)
        if page.get("status") != 200:
            raise DomainError("GitHub returned HTTP %s for this source." % page.get("status"), 502)
        entries = json.loads(page["text"])
        if not isinstance(entries, list):
            raise DomainError("This source is not a folder.")
        return [e for e in entries if isinstance(e, dict) and isinstance(e.get("name"), str) and isinstance(e.get("path"), str)]

    def head(self, info, branch, path):
        """Front matter of a remote SKILL.md -> (status, name, description). Status None on network errors."""
        try:
            page = public_page(self.raw_url(info, branch, path), max_chars=HEAD_BYTES)
        except DomainError:
            return None, "", ""
        if page.get("status") != 200:
            return page.get("status"), "", ""
        return 200, *parse_front(page.get("text", ""))

    def index_source(self, source):
        info = parse_github(source["url"])
        if not info:
            raise DomainError("Unsupported source URL.")
        branch = info["branch"] or "main"
        label, sid = source["label"], source["id"]

        def item(path, name, description):
            return {"ref": "third_party:%s:%s" % (sid, path), "name": name, "description": description, "source": "third_party",
                    "origin": label, "source_id": sid}
        if info["file"]:
            status, name, description = self.head(info, branch, info["path"])
            folder = PurePosixPath(info["path"]).parent.name
            return [item(info["path"], name or folder or info["repo"], description)]
        entries = self.list_dir(info, info["path"])
        names = {e["name"] for e in entries}
        if "SKILL.md" in names:
            status, name, description = self.head(info, branch, (info["path"] + "/" if info["path"] else "") + "SKILL.md")
            return [item((info["path"] + "/" if info["path"] else "") + "SKILL.md", name or info["path"].rsplit("/", 1)[-1] or info["repo"], description)]
        if not info["path"] and any(e["name"] == "skills" and e.get("type") == "dir" for e in entries):
            entries = self.list_dir(info, "skills")
        dirs = [e for e in entries if e.get("type") == "dir" and not e["name"].startswith(".") and e["name"].casefold() not in SKIP_DIRS]
        dirs = dirs[:MAX_ITEMS]
        pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="skill-index")
        futures = {d["path"]: pool.submit(self.head, info, branch, d["path"] + "/SKILL.md") for d in dirs[:60]}
        wait(list(futures.values()), timeout=15)
        pool.shutdown(wait=False, cancel_futures=True)
        results = []
        for d in dirs:
            name = description = ""
            hit = futures.get(d["path"])
            if hit is not None and hit.done() and not hit.cancelled():
                try:
                    status, name, description = hit.result()
                except Exception:
                    status = None
                if status is not None and status != 200:
                    continue  # a folder without SKILL.md is not a skill
            results.append(item(d["path"] + "/SKILL.md", name or d["name"], description))
        return results

    def third_party(self, only=None, fetch=True):
        """Indexed third-party skills. `fetch=False` uses only what is already cached (no network)."""
        items, errors = [], {}
        for source in self.sources():
            if only and only != source["id"]:
                continue
            key = (source["id"], source["url"])
            with self.lock:
                cached = self.remote_cache.get(key)
            if cached and time.monotonic() - cached[0] < (REMOTE_TTL if not cached[2] else 60):
                found, error = cached[1], cached[2]
            elif not fetch:
                continue
            else:
                try:
                    found, error = self.index_source(source), ""
                except (DomainError, ValueError, KeyError):
                    found, error = [], "Cannot read this source right now. Check the URL or try again later."
                with self.lock:
                    self.remote_cache[key] = (time.monotonic(), found, error)
            items.extend(found)
            if error:
                errors[source["id"]] = error
        return items, errors

    # ---- listing -------------------------------------------------------------------------
    def local_items(self):
        installed = self.installed_by_name()
        stored = self.store.all("skills")
        items = []
        for skill in stored:
            if skill.get("archived"):
                continue
            kind = "installed" if skill.get("installed") else "library"
            items.append({"ref": kind + ":" + skill["id"], "name": skill["name"], "description": skill.get("description", ""),
                          "source": kind, "installed": bool(skill.get("installed")), "skill_id": skill["id"],
                          "origin": skill.get("source", "")})
        return items, installed

    def list(self, query="", source=""):
        query, source = str(query or "").strip().casefold(), str(source or "").strip()
        kind, _, sub = source.partition(":")
        if kind and kind not in {k for k, _ in KIND_LABELS}:
            raise DomainError("Unknown skill source.")
        everything, installed = self.local_items()
        errors = {}
        for local in ("claude", "codex"):  # cheap and cached: always scanned so the per-source counts stay complete
            everything.extend(self.scan_local(local))
        fetch = not kind or kind == "third_party"
        found, errors = self.third_party(sub or None if fetch else None, fetch=fetch)
        everything.extend(found)
        everything = [dict(i) for i in everything]  # cached entries stay pristine
        words = query.split()

        def matches(item):
            haystack = (item["name"] + " " + item.get("description", "")).casefold()
            return all(w in haystack for w in words)
        for item in everything:
            if item["source"] not in ("installed", "library"):
                match = installed.get(item["name"].casefold())
                item["installed"] = bool(match)
                if match:
                    item["skill_id"] = match["id"]
        found = [i for i in everything if matches(i)]
        counts = {}
        for item in found:
            counts[item["source"]] = counts.get(item["source"], 0) + 1
        items = [i for i in found if not kind or i["source"] == kind and (not sub or i.get("source_id") == sub)]
        # Installed first within each source, then alphabetical.
        order = {k: n for n, (k, _) in enumerate(KIND_LABELS)}
        items.sort(key=lambda i: (order[i["source"]], i["name"].casefold()))
        sources = [{"id": k, "label": label, "count": counts.get(k, 0)} for k, label in KIND_LABELS
                   if k != "third_party" or self.sources() or counts.get(k)]
        result = {"items": items[:MAX_ITEMS], "sources": sources}
        if errors:
            result["errors"] = errors
        return result

    # ---- install -------------------------------------------------------------------------
    def local_file(self, kind, relative):
        parts = PurePosixPath(relative).parts
        allowed = ("skills", "plugins") if kind == "claude" else ("skills",)
        if (not parts or parts[0] not in allowed or ".." in parts or relative.startswith("/") or "\\" in relative or ":" in relative
                or parts[-1] != "SKILL.md"):
            raise DomainError("This local skill reference is not valid.", 400)
        home = self.home(kind).resolve()
        path = home.joinpath(*parts)
        cursor = home
        for part in parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise DomainError("Symlinked skills cannot be installed.", 403)
        if not path.is_file() or not path.resolve().is_relative_to(home):
            raise DomainError("This skill is no longer available on this machine.", 404)
        if path.stat().st_size > 100000:
            raise DomainError("SKILL.md exceeds 100 KB.")
        return path

    def install(self, ref, agent_id):
        ref = str(ref or "")
        agent_id = str(agent_id or "")
        if not agent_id:
            raise DomainError("Choose an agent to receive the skill.")
        agent = self.store.get("agents", agent_id)
        if agent.get("archived"):
            raise DomainError("This agent is archived.")
        kind, _, rest = ref.partition(":")
        library = self.engine.skills
        if kind in ("installed", "library"):
            skill = self.store.get("skills", rest)
            if skill.get("archived"):
                raise DomainError("This skill is archived.", 409)
            if kind == "library" or not skill.get("installed"):
                saved = library.install({"skill_id": skill["id"]}, agent_id)
            else:
                skills = list(dict.fromkeys(agent.get("skills", []) + [skill["id"]]))
                self.store.patch("agents", agent_id, {"skills": skills})
                saved = {"id": skill["id"], "name": skill["name"]}
        elif kind in ("claude", "codex"):
            path = self.local_file(kind, rest)
            content = path.read_text(encoding="utf-8", errors="replace")
            label = dict(LOCAL_SOURCES)[kind]
            # Imported third-party/local content is preserved as written (no English gate or rewrite).
            saved = library.save({"content": content, "source": label}, agent_id, preserve=True)
        elif kind == "third_party":
            sid, _, path = rest.partition(":")
            source = next((s for s in self.sources() if s["id"] == sid), None)
            parts = PurePosixPath(path).parts
            if not source or not parts or parts[-1] != "SKILL.md" or ".." in parts or path.startswith("/") or "\\" in path:
                raise DomainError("This third-party skill reference is not valid.", 400)
            info = parse_github(source["url"])
            branches = [info["branch"]] if info["branch"] else ["main", "master"]
            saved, error = None, None
            for branch in branches:
                try:
                    saved = library.save({"url": self.raw_url(info, branch, path), "source": source["label"]}, agent_id)
                    break
                except DomainError as exc:
                    error = exc
            if saved is None:
                raise error or DomainError("Cannot download this skill.", 502)
        else:
            raise DomainError("Unknown skill reference.", 400)
        self.invalidate_local()
        return {"skill_id": saved["id"], "name": saved["name"], "agent_id": agent_id}

    def invalidate_local(self):
        with self.lock:
            self.local_cache.clear()
