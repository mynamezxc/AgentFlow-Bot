import json
import difflib
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .providers import CREATE_NO_WINDOW
from .store import DomainError, now as now_iso, uid

SKIP = {".git", ".agentflow", ".env", ".venv", "node_modules", "__pycache__", ".aws", ".codex", ".claude",
        "credentials.json", "auth.json", "credentials.bin", "package-lock.json", "pnpm-lock.yaml"}
TEXT_EXTENSIONS = {".md", ".txt", ".json", ".csv", ".html", ".css", ".js", ".py", ".svg", ".toml",
                   ".yaml", ".yml", ".ts", ".tsx", ".jsx", ".xml", ".sql", ".ini", ".cfg", ".sh", ".ps1"}


class _AnyExtension:
    def __contains__(self, item):
        return True


TRASH_DAYS = 7


def permitted(relative):
    path = Path(relative)
    return not path.is_absolute() and all(
        part not in SKIP and not part.startswith(".") and not part.casefold().startswith(("secret", "credential", ".env"))
        for part in path.parts)


class FileTools:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store
        self.workspace = engine.workspace

    def validate_grant(self, path, target_type="folder", allow_create=False):
        path = Path(path)
        resolved = path.resolve()
        if target_type not in ("folder", "file"):
            raise DomainError("Scope must be folder or file.")
        if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
            raise DomainError("An absolute path on the runtime machine is required.")
        if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()) for p in [path, *path.parents]):
            raise DomainError('Access cannot be granted through symlinks or junctions.', 403)
        if target_type == "folder" and not resolved.is_dir() and not (allow_create and not resolved.exists()):
            raise DomainError("The folder does not exist on the runtime machine. Atlas needs the owner to confirm the path.", 404)
        if target_type == "file" and (not resolved.parent.is_dir() or resolved.exists() and not resolved.is_file()):
            raise DomainError("Invalid local file path.", 404)
        if resolved == Path(resolved.anchor) or resolved == Path.home() or resolved.is_relative_to(self.store.directory):
            raise DomainError("Grant a specific project folder, rather than a drive, home directory or runtime store.")
        if any(part.casefold() in {".aws", ".ssh", ".codex", ".claude", ".agentflow"} for part in resolved.parts):
            raise DomainError("Account stores and secrets cannot be granted to project tools.", 403)
        if target_type == "file" and (not permitted(resolved.name) or resolved.suffix.casefold() not in TEXT_EXTENSIONS):
            raise DomainError("Only ordinary text or source files can be granted. Secret files are excluded.", 403)
        return resolved

    def mkdir(self, args, agent, run, project=False):
        root = self.root(args, write=True) if project else self.workspace
        path = self.path(args.get('path', ''), root)
        if path == root:
            raise DomainError('Choose a subfolder to create.')
        path.mkdir(parents=True, exist_ok=True)
        self.used(args, run, project)
        return {'created': True, 'path': str(path), 'is_directory': path.is_dir()}

    def root(self, args, write=False):
        if not args.get("project_id"):
            return self.workspace
        project = self.store.get("projects", args["project_id"])
        grants = project.get("folder_grants", [])
        if not grants:
            raise DomainError("No project folder has been granted. Ask the owner through decisions.ask kind=folder_access rather than requesting each file.", 403)
        grant = next((g for g in grants if g.get("id") == args.get("grant_id")), None) if args.get("grant_id") else grants[0]
        if not grant:
            raise DomainError("This folder grant does not belong to this project.", 403)
        if write and grant.get("access") != "readwrite":
            raise DomainError("This project folder is read only. Write documents in company or request write access from the owner.", 403)
        root = Path(grant["path"]).resolve()
        if not root.is_dir():
            raise DomainError("The project folder does not exist on the runtime machine.", 404)
        if grant.get("target_type") == "file":
            name = args.get("path") or grant["file"]
            if self.path(name, root) != root / grant["file"]:
                raise DomainError("Only the explicitly granted file may be accessed, excluding other files in its folder.", 403)
            args["path"] = grant["file"]
            args["_file_scope"] = grant["file"]
        return root

    def path(self, name, root=None, extensions=None):
        root = (root or self.workspace).resolve()
        relative = Path(str(name))
        if relative.suffix and relative.suffix.lower() not in (extensions or TEXT_EXTENSIONS):
            raise DomainError("Unsupported file format. Use a text or source file.", 403)
        if not permitted(relative) or ".." in relative.parts:
            raise DomainError("Only ordinary files in the granted folder are allowed. Secrets and hidden folders are excluded.", 403)
        full = (root / relative).resolve()
        if not full.is_relative_to(root):
            raise DomainError("The path is outside the granted folder.", 403)
        cursor = root
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction()):
                raise DomainError("Symlinks and junctions cannot be accessed.", 403)
        return full

    def list_files(self, root, subpath="", limit=180):
        start = self.path(subpath, root)
        if start.is_file():
            return [{"path": str(start.relative_to(root)), "bytes": start.stat().st_size}]
        results, todo = [], [start]
        while todo and len(results) < limit:
            folder = todo.pop(0)
            try:
                for entry in sorted(folder.iterdir()):
                    relative = entry.relative_to(root)
                    if not permitted(relative) or entry.is_symlink() or (hasattr(entry, "is_junction") and entry.is_junction()):
                        continue
                    if entry.is_dir():
                        todo.append(entry)
                    elif entry.suffix.lower() in TEXT_EXTENSIONS:
                        results.append({"path": str(relative), "bytes": entry.stat().st_size})
                    if len(results) >= limit:
                        break
            except (PermissionError, OSError):
                continue
        return results

    READ_DEFAULT = 12000
    READ_MAX = 20000
    OUTLINE_RE = re.compile(r"^\s{0,8}(?:#{1,4}\s+\S|(?:async\s+)?def\s+\w|class\s+\w|(?:export\s+)?(?:default\s+)?(?:async\s+)?function\b|export\s+(?:const|class|default|interface|type|enum)\b|(?:pub\s+)?(?:fn|struct|enum|impl|trait)\s+\w|func\s+\w|<h[1-3][\s>]|@media\b|\[[\w.\"-]+\]\s*$)")

    @classmethod
    def outline(cls, content, after, limit=1800, max_entries=60):
        """Compact jump table for a large file: '@<char offset> L<line> <heading/def/class>' beyond the first chunk."""
        entries, offset, size = [], 0, 0
        for number, line in enumerate(content.splitlines(keepends=True), 1):
            if offset >= after and cls.OUTLINE_RE.match(line):
                item = "@%d L%d %s" % (offset, number, " ".join(line.split())[:70])
                if size + len(item) > limit or len(entries) >= max_entries:
                    entries.append("…more entries omitted; use project.search for a name")
                    break
                entries.append(item)
                size += len(item) + 1
            offset += len(line)
        return entries

    def fingerprint(self, args, project=False):
        """(resolved path, size, mtime_ns) of the file a read would return, or None (directory, listing, error).
        Never raises; used to detect an unchanged re-read."""
        try:
            args = dict(args)
            if project and not args.get("project_id"):
                return None
            root = self.root(args) if project else self.workspace
            if not args.get("path"):
                return None
            path = self.path(args["path"], root)
            if not path.is_file() or path.suffix.casefold() not in TEXT_EXTENSIONS:
                return None
            info = path.stat()
            return (str(path), info.st_size, info.st_mtime_ns)
        except (DomainError, OSError, ValueError, KeyError, TypeError):
            return None

    def read(self, args, project=False, full=False):
        root = self.root(args) if project else self.workspace
        if not args.get("path"):
            return {"files": self.list_files(root), "root": str(root)}
        path = self.path(args["path"], root)
        if path.is_dir():
            return {"files": self.list_files(root, args["path"]), "root": str(path)}
        if path.suffix.casefold() not in TEXT_EXTENSIONS or not path.is_file():
            raise DomainError("No supported text file found.", 404)
        if path.stat().st_size > 3000000:
            raise DomainError("This file exceeds 3 MB. Use project.search to find the relevant content.")
        content = path.read_text(encoding="utf-8", errors="replace")
        offset = max(0, int(args.get("offset") or 0))
        if full:
            limit = 3000000
        else:
            limit = min(self.READ_MAX, max(100, int(args.get("limit") or self.READ_DEFAULT)))
        result = {"path": str(path.relative_to(root)), "content": content[offset:offset + limit], "total_chars": len(content),
                  "next_offset": offset + limit if len(content) > offset + limit else None}
        if full:
            result["total_characters"] = len(content)
        elif len(content) > self.READ_MAX and offset == 0:
            result["outline"] = self.outline(content, limit)
        return result

    SEARCH_MAX_MATCHES = 40
    SEARCH_LINE = 200
    SEARCH_TOTAL = 6000

    def search(self, args, project=False):
        root = self.root(args) if project else self.workspace
        query = str(args.get("query", "")).casefold()
        if not query:
            raise DomainError("Search content is required.")
        result, total = [], 0
        for info in self.list_files(root, args.get("path", ""), 1500):
            if info["bytes"] > 1000000:
                continue
            path = self.path(info["path"], root)
            for line, text in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                folded = text.casefold()
                if query in folded:
                    text = text.strip()
                    if len(text) > self.SEARCH_LINE:  # keep the matching part visible
                        at = max(0, text.casefold().find(query) - 60)
                        text = ("…" if at else "") + text[at:at + self.SEARCH_LINE - 1] + "…"
                    size = len(info["path"]) + len(text) + 24
                    if len(result) >= self.SEARCH_MAX_MATCHES or total + size > self.SEARCH_TOTAL:
                        result.append({"note": "More matches exist; narrow the query or path."})
                        return result
                    total += size
                    result.append({"path": info["path"], "line": line, "text": text})
        return result

    def used(self,args,run,project=False):
        if not run.get("project_id"): return
        root=self.root(args) if project else self.workspace
        path=self.path(args.get("path", ""),root)
        folder=path if path.is_dir() else path.parent
        with self.store.transaction():
            record=self.store.get("projects",run["project_id"])
            paths=record.get("context_paths",[])
            if str(folder) not in paths:
                self.store.patch("projects",record["id"],{"context_paths":(paths+[str(folder)])[-100:]})

    def write(self, args, agent, run, project=False):
        with self.store.transaction():
            result=self._write(args,agent,run,project)
            self.used(args,run,project)
            return result

    def _write(self, args, agent, run, project=False):
        root = self.root(args, write=True) if project else self.workspace
        path = self.path(args.get("path", ""), root)
        if path == root or path.suffix.casefold() not in TEXT_EXTENSIONS:
            raise DomainError("Choose a text file path with a supported extension.")
        content = str(args.get("content", ""))
        if len(content.encode()) > 500000:
            raise DomainError("A single write may contain up to 500 KB.")
        before = None
        if path.exists():
            if path.stat().st_size>3000000: raise DomainError("The original file exceeds 3 MB. Split the change to record an accurate diff.")
            before=self.engine.vault.redact(path.read_text(encoding="utf-8",errors="replace"))
        after=self.engine.vault.redact(content)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".agentflow-tmp")
        temporary.write_text(after, encoding="utf-8")
        temporary.replace(path)
        artifact = next((a for a in self.store.all("artifacts") if a.get("absolute_path") == str(path)), None)
        doc = {"title": str(args.get("title") or path.name)[:250], "path": str(path.relative_to(root)),
               "absolute_path": str(path), "project_id": run.get("project_id"), "agent_id": agent["id"],
               "run_id": run["id"], "bytes": path.stat().st_size, "type": path.suffix.lstrip("."),
               "sha256":hashlib.sha256(path.read_bytes()).hexdigest(),
               "source": "project" if project else "workspace", "grant_id": args.get("grant_id"),
               "source_project": args.get("project_id") if project else None,
               **({"deliverable": True} if args.get("deliverable") is True else {})}
        saved = self.store.patch("artifacts", artifact["id"], doc) if artifact else self.store.create("artifacts", doc)
        change=None
        if before!=after:
            added=deleted=0
            for tag,i,j,k,l in difflib.SequenceMatcher(None,(before or "").splitlines(),after.splitlines()).get_opcodes():
                if tag in ("insert","replace"):added+=l-k
                if tag in ("delete","replace"):deleted+=j-i
            change=self.store.create("file_changes",{"artifact_id":saved["id"],"project_id":run.get("project_id"),
                "run_id":run["id"],"agent_id":agent["id"],"path":saved["path"],"title":saved["title"],
                "kind":"created" if before is None else "modified","before":before,"after":after,
                "added":added,"deleted":deleted,"before_sha256":hashlib.sha256((before or "").encode()).hexdigest(),
                "after_sha256":hashlib.sha256(after.encode()).hexdigest()})
        return {**{key: saved[key] for key in ("id", "title", "path", "bytes", "project_id")},"change_id":change["id"] if change else None}

    def change(self, change_id):
        item=self.store.get("file_changes",change_id)
        lines=[];before=(item.get("before") or "").splitlines();after=item["after"].splitlines()
        for group in difflib.SequenceMatcher(None,before,after).get_grouped_opcodes(3):
            lines.append({"kind":"hunk","text":"@@ -"+str(group[0][1]+1)+","+str(group[-1][2]-group[0][1])+" +"+str(group[0][3]+1)+","+str(group[-1][4]-group[0][3])+" @@"})
            for tag,i,j,k,l in group:
                if tag=="equal":lines.extend({"kind":"context","old":i+n+1,"new":k+n+1,"text":text} for n,text in enumerate(before[i:j]))
                if tag in ("delete","replace"):lines.extend({"kind":"delete","old":i+n+1,"new":None,"text":text} for n,text in enumerate(before[i:j]))
                if tag in ("insert","replace"):lines.extend({"kind":"add","old":None,"new":k+n+1,"text":text} for n,text in enumerate(after[k:l]))
        return {**{k:v for k,v in item.items() if k not in ("before","after")},"before":item.get("before"),"after":item["after"],"diff":lines[:1500],"diff_truncated":len(lines)>1500}

    DIFF_LIMIT = 12000

    def change_view(self, item, offset=0, limit=None):
        """Compact model-facing view of a file revision: stats plus a unified diff, capped at 12k characters."""
        limit = min(self.DIFF_LIMIT, max(500, int(limit or self.DIFF_LIMIT)))
        name = item.get("path") or "file"
        before = (item.get("before") or "").splitlines()
        after = (item.get("after") or "").splitlines()
        diff = "\n".join(difflib.unified_diff(before, after, "a/" + name, "b/" + name, n=2, lineterm=""))
        offset = max(0, int(offset or 0))
        result = {"change_id": item["id"], "path": name, "title": item.get("title"), "kind": item.get("kind"),
                  "added": item.get("added"), "deleted": item.get("deleted"), "diff_chars": len(diff),
                  "diff": diff[offset:offset + limit]}
        if len(diff) > offset + limit:
            result["next_offset"] = offset + limit
            result["note"] = "Diff truncated at %d of %d chars; pass offset for the rest or read the file for its current content." % (offset + limit, len(diff))
        return result

    def execute(self, args, run_id):
        docker = shutil.which("docker")
        if not docker:
            raise DomainError("Docker CLI is unavailable. Reading and writing documents is still possible. Do not claim tests ran. Ask the owner to enable Docker if execution is required.")
        command = args.get("command")
        if not isinstance(command, list) or not command or len(command) > 40 or not all(isinstance(v, str) and len(v) < 2000 for v in command):
            raise DomainError("Execution requires an argv array rather than a host shell command.")
        root = self.root(args) if args.get("project_id") else self.workspace
        stage_parent = self.store.directory / "execution"
        stage_parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=stage_parent) as stage:
            staged = Path(stage)
            copied = 0
            for info in self.list_files(root, args.get("_file_scope", ""), limit=3000):
                if copied + info["bytes"] > 30000000:
                    break
                target = staged / info["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.path(info["path"], root), target)
                copied += info["bytes"]
            image = self.store.settings().get("execution_image", "python:3.12-slim")
            argv = [docker, "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges", "--memory", "512m", "--cpus", "1", "--pids-limit", "128",
                    "--tmpfs", "/tmp:rw,nosuid,size=128m", "--mount", f"type=bind,source={stage},target=/work",
                    "--workdir", "/work", image] + command
            process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
            self.engine.providers.processes[run_id] = process
            try:
                stdout, stderr = process.communicate(timeout=min(300, max(5, int(args.get("timeout", 60)))))
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise DomainError("Execution timed out. Tests have not been marked as passed.") from None
            finally:
                self.engine.providers.processes.pop(run_id, None)
            return {"executed": True, "isolated": "Docker; network=none; sanitized copy",
                    "exit_code": process.returncode, "stdout": self.engine.vault.redact(stdout[-20000:]),
                    "stderr": self.engine.vault.redact(stderr[-10000:]), "source_unchanged": True}

    # ---- safe deletion: agents may only trash files that AgentFlow agents created -----------------
    def provenance(self):
        """({absolute_path: [artifacts]}, {artifact_id: {change kinds}}) used to decide who authored a file."""
        by_path, kinds = {}, {}
        for artifact in self.store.all("artifacts"):
            by_path.setdefault(artifact.get("absolute_path"), []).append(artifact)
            # Company files are also indexed by their current location, so renaming/moving the AgentFlow
            # folder does not turn agent-created files into "owner files".
            if artifact.get("source", "workspace") != "project" and artifact.get("path"):
                try:
                    current = str((self.workspace / artifact["path"]).resolve())
                except (OSError, ValueError):
                    current = None
                if current and current != artifact.get("absolute_path"):
                    by_path.setdefault(current, []).append(artifact)
        for change in self.store.all("file_changes"):
            kinds.setdefault(change.get("artifact_id"), set()).add(change.get("kind"))
        return by_path, kinds

    def agent_created(self, full, root, index):
        """True when `full` is a scratch file or was created (not merely modified) by an agent."""
        relative = Path(full).relative_to(root)
        if "scratch" in relative.parts[:-1] or relative.parts[:1] == ("scratch",) and Path(full).is_dir():
            return True
        by_path, kinds = index
        artifacts = by_path.get(str(full), [])
        seen = set().union(*(kinds.get(a["id"], set()) for a in artifacts)) if artifacts else set()
        if "created" in seen:
            return True
        if seen:
            return False  # an agent only modified a file that already existed: it belongs to the owner
        return any(a.get("agent_id") and not a.get("external") for a in artifacts)

    def delete(self, args, agent, run, project=False):
        """Move files or folders to <store>/trash/<date>/<run>/<id>/ with a restore manifest.

        Granted project folder: any file, only with a readwrite grant. Company workspace: agent-created or scratch/ files."""
        raw = args.get("paths") if args.get("paths") is not None else args.get("path")
        raw = [raw] if isinstance(raw, str) else raw
        if not isinstance(raw, list) or not raw or len(raw) > 50 or not all(isinstance(p, str) and p.strip() for p in raw):
            raise DomainError("Give path (or paths, up to 50) of files or folders you created.")
        root = self.root(args, write=True) if project else self.workspace
        index = self.provenance()
        targets, refused = [], []
        for name in dict.fromkeys(p.strip() for p in raw):
            full = self.path(name, root, extensions=_AnyExtension())
            if full == root:
                raise DomainError("Choose a file or folder inside the root, not the root itself.")
            if not os.path.lexists(full):
                refused.append(name + ": not found")
                continue
            if full.is_symlink() or (hasattr(full, "is_junction") and full.is_junction()):
                refused.append(name + ": symlinks cannot be deleted")
                continue
            members = [full]
            if full.is_dir():
                members = []
                for top, dirs, names in os.walk(full, followlinks=False):
                    for entry in list(dirs) + names:
                        member = Path(top) / entry
                        if member.is_symlink() or (hasattr(member, "is_junction") and member.is_junction()):
                            refused.append(name + ": contains a link (" + entry + ")")
                            dirs[:] = []
                            break
                        if entry in names:
                            members.append(member)
            # Owner decision: a readwrite folder grant (enforced by root(write=True)) covers deleting any file in it,
            # e.g. for coding agents; files still go to the restorable trash. The company workspace keeps the
            # agent-created/scratch rule because the owner never granted it explicitly.
            if sum(1 for m in members if m.is_file()) > 500:
                refused.append(name + ": more than 500 files; delete smaller folders or ask the owner")
                continue
            owner_files = [] if project else [m for m in members if m.is_file() and not self.agent_created(m, root, index)]
            if owner_files:
                shown = ", ".join(str(m.relative_to(root)).replace("\\", "/") for m in owner_files[:3])
                refused.append(name + ": not created by an agent (" + shown + (" ..." if len(owner_files) > 3 else "") +
                               "). Only files you created or files under scratch/ can be deleted; ask the owner for anything else.")
            elif not project and not full.is_dir() and not self.agent_created(full, root, index):
                refused.append(name + ": not created by an agent. Only files you created or files under scratch/ can be deleted.")
            else:
                targets.append(full)
        if refused:
            raise DomainError("Nothing was deleted. " + "; ".join(refused), 403)
        run_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(run.get("id") or "run"))[:80]
        trash_id = uid("trash")
        base = self.store.directory / "trash" / time.strftime("%Y%m%d") / run_id / trash_id
        items, moved_bytes = [], 0
        for full in targets:
            relative = full.relative_to(root)
            destination = base / "files" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            size = full.stat().st_size if full.is_file() else sum(f.stat().st_size for f in full.rglob("*") if f.is_file())
            shutil.move(str(full), str(destination))
            moved_bytes += size
            items.append({"original_path": str(relative).replace("\\", "/"), "absolute_path": str(full),
                          "is_directory": destination.is_dir(), "bytes": size})
        manifest = {"id": trash_id, "deleted_at": now_iso(), "deleted_epoch": time.time(), "run_id": run.get("id"),
                    "agent_id": agent.get("id"), "source": "project" if project else "workspace",
                    "project_id": args.get("project_id") if project else None, "grant_id": args.get("grant_id") if project else None,
                    "root": str(root), "items": items}
        (base / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        trashed = [str(i["absolute_path"]) for i in items]
        for artifact in self.store.all("artifacts"):
            where = str(artifact.get("absolute_path") or "")
            if any(where == t or where.startswith(t + os.sep) for t in trashed):
                self.store.patch("artifacts", artifact["id"], {"trashed_at": manifest["deleted_at"], "trash_id": trash_id})
        return {"deleted": [i["original_path"] for i in items], "trash_id": trash_id, "bytes": moved_bytes,
                "note": "Moved to trash; the owner can restore it for %d days." % TRASH_DAYS}

    def trash_manifests(self):
        found = []
        for manifest in sorted((self.store.directory / "trash").glob("*/*/*/manifest.json")):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("id"):
                    found.append((manifest.parent, data))
            except (OSError, ValueError):
                continue
        return found

    def purge_trash(self, days=TRASH_DAYS):
        """Permanently remove trash entries older than `days`. Never raises."""
        freed = count = 0
        try:
            from .housekeeping import force_rmtree
            cutoff = time.time() - max(0, float(days)) * 86400
            for folder, data in self.trash_manifests():
                if float(data.get("deleted_epoch") or folder.stat().st_mtime) > cutoff:
                    continue
                freed += sum(int(i.get("bytes") or 0) for i in data.get("items", []))
                count += sum(1 for _ in data.get("items", []))
                force_rmtree(folder)
                for parent in (folder.parent, folder.parent.parent):
                    try:
                        parent.rmdir()
                    except OSError:
                        break
        except Exception:
            pass
        return {"freed_bytes": freed, "files": count}

    def restore(self, trash_id):
        """Put a trashed entry back where it was (refuses to overwrite anything)."""
        entry = next(((f, d) for f, d in self.trash_manifests() if d["id"] == str(trash_id)), None)
        if not entry:
            raise DomainError("This trash entry no longer exists.", 404)
        folder, data = entry
        root = Path(data["root"])
        planned = []
        for item in data["items"]:
            target = (root / item["original_path"]).resolve()
            if not target.is_relative_to(root.resolve()):
                raise DomainError("The original location is no longer valid.", 403)
            if target.exists():
                raise DomainError("Something already exists at " + item["original_path"] + ". Rename it first.", 409)
            planned.append((folder / "files" / item["original_path"], target))
        for source, target in planned:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
        for artifact in self.store.all("artifacts"):
            if artifact.get("trash_id") == data["id"]:
                self.store.patch("artifacts", artifact["id"], {"trashed_at": None, "trash_id": None})
        from .housekeeping import force_rmtree
        force_rmtree(folder)
        for parent in (folder.parent, folder.parent.parent):
            try:
                parent.rmdir()
            except OSError:
                break
        return {"restored": [i["original_path"] for i in data["items"]], "trash_id": data["id"]}

    def trash_list(self):
        return [{"id": d["id"], "deleted_at": d.get("deleted_at"), "run_id": d.get("run_id"), "agent_id": d.get("agent_id"),
                 "source": d.get("source"), "items": [i["original_path"] for i in d.get("items", [])]}
                for _, d in self.trash_manifests()]
