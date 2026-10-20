"""Disk housekeeping: safe automatic cleanup plus ask-first cleanup when space is short.

AUTO (no asking, ``housekeeping.auto_cleanup``): stale temp files, desktop-open copies, surplus
prompt files, native CLI session files AgentFlow created, SQLite history compaction.
ASK FIRST (one non-blocking Atlas decision, 24 h snooze): old reset backups, unreferenced browser
screenshots, attachments of finished projects, and optional caches (browser, parsers).

Owner deliverables are never touched: registered artifacts other than screenshot evidence,
files in the company workspace, attachments of active projects. Every deletion is re-verified to
sit inside its category's allowed root and never follows or removes symlinks/junctions.
"""
import json
import os
import re
import shutil
import stat
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .contract import TERMINAL
from .store import DomainError, now

GB, MB = 1024 ** 3, 1024 ** 2
DEFAULTS = {"auto_cleanup": True, "min_free_gb": 5, "retention_days": 7}
RUN_INTERVAL = 6 * 3600
ASK_SNOOZE = 24 * 3600
DATA_LIMIT = 2 * GB
MIN_ASK_BYTES = 50 * MB
PROMPT_KEEP = 50
ANCHOR_RUN = "run-housekeeping"
AUTO = ("temp", "open_files", "prompts", "native_sessions", "database")
ASK = ("reset_archives", "screenshots", "attachments", "browser_cache", "parser_packages")
LABELS = {
    "temp": ("Temporary files", "Leftover .tmp files from interrupted writes."),
    "open_files": ("Opened document copies", "Copies made when you opened a document in a desktop app, older than 1 day."),
    "prompts": ("Prompt cache", "Saved prompt files beyond the newest %d." % PROMPT_KEEP),
    "native_sessions": ("CLI sessions", "Codex / Claude Code session files AgentFlow created for finished runs."),
    "database": ("Run history", "Step traces and old tool output of finished runs. Results and summaries stay."),
    "reset_archives": ("Reset backups", "Backups made by Reset-AgentFlow, older than 7 days."),
    "screenshots": ("Browser screenshots", "Screenshots older than the retention period that no chat message uses."),
    "attachments": ("Old attachments", "Uploads of finished or archived projects older than 30 days, and unsent uploads."),
    "browser_cache": ("Browser engine", "Playwright browser and packages. Reinstalled on demand. Only offered when unused for 30 days."),
    "parser_packages": ("Document readers", "PDF reader packages. Reinstalled on demand. Only offered when unused for 30 days."),
}
RESET_NAME = re.compile(r"^reset-(\d{8})-(\d{6})-\d{3}$")
SCREENSHOT = re.compile(r"^screenshot-[0-9a-f]{12}\.png$")
SCREENSHOT_REF = re.compile(r"screenshot-[0-9a-f]{12}")


def fmt(size):
    size = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return ("%d %s" % (size, unit)) if unit == "B" else ("%.1f %s" % (size, unit))
        size /= 1024


def iso_epoch(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def is_link(path):
    junction = getattr(os.path, "isjunction", None)
    return os.path.islink(path) or bool(junction and junction(path))


def measure(path):
    """(bytes, files, links) below `path` without following links."""
    path = str(path)
    try:
        if is_link(path):
            return 0, 0, 1
        if os.path.isfile(path):
            return os.stat(path).st_size, 1, 0
    except OSError:
        return 0, 0, 0
    size = files = links = 0
    for top, dirs, names in os.walk(path, followlinks=False):
        for name in list(dirs):
            if is_link(os.path.join(top, name)):
                links += 1
                dirs.remove(name)
        for name in names:
            full = os.path.join(top, name)
            if is_link(full):
                links += 1
                continue
            try:
                size += os.stat(full).st_size
                files += 1
            except OSError:
                continue
    return size, files, links


def force_rmtree(path):
    """rmtree that also removes read-only files (browser caches on Windows)."""
    def retry(function, target, *_):
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            function(target)
        except OSError:
            pass
    shutil.rmtree(path, **({"onexc": retry} if sys.version_info >= (3, 12) else {"onerror": retry}))


def mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return time.time()  # unreadable or vanished: treat as brand new, never a candidate


class Housekeeping:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self.path = self.store.directory / "housekeeping.json"
        self.lock = threading.RLock()
        self.work = threading.Lock()
        self.thread = None
        self.started = time.monotonic()
        self.startup_delay = 60
        self._cache = {}
        try:
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self.state, dict):
                self.state = {}
        except (OSError, ValueError):
            self.state = {}
        engine.decision_handlers["cleanup"] = self.decision

    # ---- configuration and state ----------------------------------------------------------
    @staticmethod
    def validate(data, current=None):
        if not isinstance(data, dict):
            raise DomainError("Invalid housekeeping settings.")
        merged = {**DEFAULTS, **{k: v for k, v in (current or {}).items() if k in DEFAULTS}}
        if "auto_cleanup" in data:
            if not isinstance(data["auto_cleanup"], bool):
                raise DomainError("Automatic cleanup must be on or off.")
            merged["auto_cleanup"] = data["auto_cleanup"]
        if "min_free_gb" in data:
            value = data["min_free_gb"]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 500:
                raise DomainError("Minimum free space must be between 1 and 500 GB.")
            merged["min_free_gb"] = value
        if "retention_days" in data:
            value = data["retention_days"]
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 365:
                raise DomainError("Retention must be between 1 and 365 days.")
            merged["retention_days"] = value
        return merged

    def config(self):
        try:
            return self.validate(self.store.settings().get("housekeeping") or {}, {})
        except DomainError:
            return dict(DEFAULTS)

    def save_state(self):
        with self.lock:
            try:
                temporary = self.path.with_suffix(".tmp")
                temporary.write_text(json.dumps(self.state), encoding="utf-8")
                temporary.replace(self.path)
            except OSError:
                pass

    # ---- locations ------------------------------------------------------------------------
    def artifact_roots(self):
        found = []
        for base in (self.store.directory.parent, self.engine.workspace.parent.parent):
            candidate = base / "artifacts"
            if candidate.is_dir() and not is_link(candidate) and candidate not in found:
                found.append(candidate)
        return found

    def dirs(self):
        directory, workspace = self.store.directory, self.engine.workspace
        return {"store": directory, "open": directory / "open-files", "prompts": directory / "prompts",
                "attachments": directory / "attachments", "browsers": directory / "browsers",
                "browser_packages": directory / "browser-packages", "parser": directory / "parser-packages",
                "media": workspace / "media"}

    def db_files(self):
        return [self.store.directory / ("workspace.sqlite3" + suffix) for suffix in ("", "-wal", "-shm")]

    def db_bytes(self):
        total = 0
        for file in self.db_files():
            try:
                total += file.stat().st_size
            except OSError:
                pass
        return total

    def allowed(self, path, roots):
        try:
            resolved = Path(path).resolve()
            return any(resolved != root.resolve() and resolved.is_relative_to(root.resolve()) for root in roots)
        except (OSError, ValueError):
            return False

    def remove(self, path, roots):
        """Delete one file or directory tree. Returns (bytes, files) or None when refused/failed."""
        path = Path(path)
        if not os.path.lexists(path) or is_link(path) or not self.allowed(path, roots):
            return None
        try:
            size, files, links = measure(path)
            if links:
                return None  # never delete trees that contain links which may point elsewhere
            if path.is_dir():
                force_rmtree(path)
                left = measure(path)[0] if path.exists() else 0
                return max(0, size - left), files
            path.unlink()
            return size, 1
        except OSError:
            return None

    # ---- activity -------------------------------------------------------------------------
    def idle(self):
        if self.engine.active or self.engine.providers.processes:
            return False
        with self.store.lock:
            return not self.store.conn.execute(
                "SELECT 1 FROM records WHERE kind='runs' AND payload LIKE '%\"status\": \"running\"%' LIMIT 1").fetchone()

    def live_run_ids(self, grace=3600):
        keep, current = set(), time.time()
        for run in self.store.all("runs"):
            if run["status"] not in TERMINAL:
                keep.add(run["id"])
                continue
            finished = iso_epoch(run.get("completed_at") or run.get("updated_at"))
            if finished is None or current - finished < grace:
                keep.add(run["id"])
        return keep

    # ---- candidate collectors (never delete) ----------------------------------------------
    def _entries(self, directory):
        try:
            return [p for p in Path(directory).iterdir()]
        except OSError:
            return []

    def _item(self, category, path, root, record=None):
        size, files, links = measure(path)
        if links:
            return None
        return {"category": category, "path": str(path), "root": str(root), "bytes": size, "files": files, "record": record}

    def collect(self, category, retention=None):
        """(items, used_bytes) for one category."""
        cfg = self.config()
        days = retention if retention is not None else cfg["retention_days"]
        d, current, items, used = self.dirs(), time.time(), [], 0
        if category == "temp":
            for folder in (d["store"], d["prompts"]):
                for p in self._entries(folder):
                    if p.suffix == ".tmp" and p.is_file() and not is_link(p) and current - mtime(p) > 3600:
                        items.append(self._item(category, p, folder))
        elif category == "open_files":
            used = measure(d["open"])[0]
            items = [self._item(category, p, d["open"]) for p in self._entries(d["open"])
                     if not is_link(p) and current - mtime(p) > 86400]
        elif category == "prompts":
            used = measure(d["prompts"])[0]
            files = sorted((p for p in self._entries(d["prompts"]) if p.is_file() and p.suffix != ".tmp" and not is_link(p)),
                           key=mtime, reverse=True)
            busy = not self.idle()
            for p in files[PROMPT_KEEP:]:
                # A live run may still read an older content-addressed prompt: be conservative while busy.
                if busy and current - mtime(p) < 86400:
                    continue
                items.append(self._item(category, p, d["prompts"]))
        elif category == "reset_archives":
            for root in self.artifact_roots():
                for p in self._entries(root):
                    match = RESET_NAME.match(p.name)
                    if not match or not p.is_dir() or is_link(p):
                        continue
                    try:
                        stamp = datetime.strptime(match[1] + match[2], "%Y%m%d%H%M%S").timestamp()
                    except ValueError:
                        stamp = mtime(p)
                    item = self._item(category, p, root)
                    if item:
                        used += item["bytes"]
                        if current - stamp > 7 * 86400:
                            items.append(item)
        elif category == "screenshots":
            media = d["media"]
            records = {a.get("path"): a for a in self.store.all("artifacts")}
            candidates = [p for p in self._entries(media) if SCREENSHOT.match(p.name) and p.is_file() and not is_link(p)]
            used = sum(measure(p)[0] for p in candidates)
            old = [p for p in candidates if current - mtime(p) > days * 86400]
            referenced = self._referenced_screenshots() if old else set()
            for p in old:
                record = records.get("media/" + p.name)
                if p.stem in referenced or record and (record.get("kind") != "screenshot" or record["id"] in referenced):
                    continue
                items.append(self._item(category, p, media, ("artifacts", record["id"]) if record else None))
        elif category == "attachments":
            used = measure(d["attachments"])[0]
            projects = {p["id"]: p for p in self.store.all("projects")}
            for a in self.store.all("attachments"):
                if a.get("file_removed"):
                    continue
                created = iso_epoch(a.get("created_at"))
                if created is None or current - created < 30 * 86400:
                    continue
                project = projects.get(a.get("project_id"))
                finished = project is not None and (project.get("archived") or project.get("status") == "done")
                if a.get("project_id") and not finished:
                    continue  # attachments of active projects are never offered
                path = d["attachments"] / (a["id"] + a.get("suffix", ""))
                if path.is_file() and not is_link(path):
                    items.append(self._item(category, path, d["attachments"], ("attachments", a["id"])))
        elif category in ("browser_cache", "parser_packages"):
            targets = [d["browsers"], d["browser_packages"]] if category == "browser_cache" else [d["parser"]]
            targets = [t for t in targets if t.is_dir() and not is_link(t)]
            used = sum(measure(t)[0] for t in targets)
            if targets and all(current - mtime(t) > 30 * 86400 for t in targets) and not self._recently_used(category, current):
                items = [self._item(category, t, d["store"]) for t in targets]
        return [i for i in items if i], used

    def _referenced_screenshots(self):
        found = set()
        for kind in ("messages", "posts", "tasks", "memories", "checklist", "projects", "plans", "decisions"):
            for record in self.store.all(kind):
                found.update(SCREENSHOT_REF.findall(json.dumps(record, ensure_ascii=False)))
        return found

    def _recently_used(self, category, current):
        cutoff = current - 30 * 86400
        if category == "browser_cache":
            if any((iso_epoch(e.get("created_at")) or 0) > cutoff for e in self.store.all("tool_events") if str(e.get("name", "")).startswith("browser.")):
                return True
            return any((iso_epoch(a.get("created_at")) or 0) > cutoff for a in self.store.all("artifacts") if a.get("kind") == "screenshot")
        return any((iso_epoch(a.get("created_at")) or 0) > cutoff for a in self.store.all("attachments") if a.get("suffix") == ".pdf")

    # ---- cleaners -------------------------------------------------------------------------
    def clean_items(self, items):
        freed = files = 0
        for item in items:
            if self.engine.stop.is_set():
                break
            result = self.remove(item["path"], [Path(item["root"])])
            if not result:
                continue
            freed, files = freed + result[0], files + result[1]
            record = item.get("record")
            if record:
                try:
                    self.store.patch(record[0], record[1], {"file_removed": True, "file_removed_at": now()})
                except DomainError:
                    pass
        return {"freed_bytes": freed, "files": files}

    def clean(self, category):
        if category in ("temp", "open_files", "prompts", "reset_archives", "screenshots", "attachments", "browser_cache", "parser_packages"):
            return self.clean_items(self.collect(category)[0])
        if category == "native_sessions":
            return self._clean_sessions()
        if category == "database":
            return self._clean_database(vacuum=self.idle())
        raise DomainError("Unknown storage category.", 400)

    def _sessions(self):
        return getattr(self.engine.providers, "sessions", None)

    def _clean_sessions(self):
        sessions = self._sessions()
        if not sessions or not hasattr(sessions, "prune"):
            return {"freed_bytes": 0, "files": 0}
        try:
            result = sessions.prune(keep_run_ids=self.live_run_ids(), older_than_s=3600) or {}
        except Exception:
            return {"freed_bytes": 0, "files": 0}
        return {"freed_bytes": int(result.get("freed_bytes", 0)), "files": int(result.get("files", 0))}

    def _sessions_estimate(self):
        sessions = self._sessions()
        if not sessions or not hasattr(sessions, "entries") or not hasattr(sessions, "files"):
            return 0, 0
        keep, current, size, count = self.live_run_ids(), time.time(), 0, 0
        try:
            for entry in sessions.entries():
                if entry.get("run_id") in keep or current - float(entry.get("created", 0)) < 3600:
                    continue
                for file in sessions.files(entry.get("provider"), entry.get("session_id")):
                    size, count = size + file.stat().st_size, count + 1
        except (OSError, ValueError, TypeError):
            pass
        return size, count

    def _history_ids(self):
        """Terminal runs past retention that still carry bulky step data, and old bulky tool events."""
        cutoff = time.time() - self.config()["retention_days"] * 86400
        conn, runs, events = self.store.conn, [], []
        with self.store.lock:
            run_ids = [r[0] for r in conn.execute("SELECT id FROM records WHERE kind='runs' AND length(payload)>2000")]
            event_ids = [r[0] for r in conn.execute("SELECT id FROM records WHERE kind='tool_events' AND length(payload)>2500")]
        for run_id in run_ids:
            try:
                run = self.store.get("runs", run_id)
            except DomainError:
                continue
            finished = iso_epoch(run.get("completed_at") or run.get("updated_at"))
            if run["status"] in TERMINAL and finished and finished < cutoff:
                if not run.get("housekeeping_trimmed") and (run.get("trace") or run.get("pending_calls") or run.get("operation_fingerprints")):
                    runs.append(run_id)
        for event_id in event_ids:
            try:
                event = self.store.get("tool_events", event_id)
            except DomainError:
                continue
            stamp = iso_epoch(event.get("updated_at") or event.get("created_at"))
            if event.get("status") in ("done", "error") and stamp and stamp < cutoff and not event.get("housekeeping_trimmed") \
                    and len(json.dumps(event.get("result", ""), ensure_ascii=False)) > 2048 and self._run_terminal(event.get("run_id")):
                events.append(event_id)
        return runs, events

    def _run_terminal(self, run_id):
        try:
            return self.store.get("runs", run_id)["status"] in TERMINAL
        except DomainError:
            return True  # the run record is gone; nothing can resume the event

    def _database_estimate(self):
        cached = self._cache.get("database")
        if cached and time.time() - cached[0] < 60:
            return cached[1]
        runs, events = self._history_ids()
        size = 0
        for run_id in runs:
            run = self.store.get("runs", run_id)
            size += sum(len(json.dumps(run.get(k) or [], ensure_ascii=False)) for k in ("trace", "pending_calls", "operation_fingerprints"))
        for event_id in events:
            size += max(0, len(json.dumps(self.store.get("tool_events", event_id).get("result", ""), ensure_ascii=False)) - 2048)
        value = (size, len(runs) + len(events))
        self._cache["database"] = (time.time(), value)
        return value

    def _clean_database(self, vacuum=False):
        before = self.db_bytes()
        runs, events = self._history_ids()
        trimmed = 0
        for index, run_id in enumerate(runs):
            if self.engine.stop.is_set():
                break
            with self.store.transaction():
                run = self.store.get("runs", run_id)
                if run["status"] not in TERMINAL:
                    continue
                version = run.pop("version")
                run.update(trace=[], pending_calls=[], operation_fingerprints=[], housekeeping_trimmed=True)
                self.store.conn.execute("UPDATE records SET payload=? WHERE kind='runs' AND id=?",
                                        (json.dumps(run, ensure_ascii=False), run_id))
                trimmed += 1
            if index % 50 == 49:
                time.sleep(0.01)
        for index, event_id in enumerate(events):
            if self.engine.stop.is_set():
                break
            with self.store.transaction():
                event = self.store.get("tool_events", event_id)
                text = json.dumps(event.get("result", ""), ensure_ascii=False)
                if len(text) <= 2048:
                    continue
                event.pop("version")
                event.update(result={"truncated": True, "original_bytes": len(text), "preview": text[:1500]}, housekeeping_trimmed=True)
                self.store.conn.execute("UPDATE records SET payload=? WHERE kind='tool_events' AND id=?",
                                        (json.dumps(event, ensure_ascii=False), event_id))
                trimmed += 1
            if index % 50 == 49:
                time.sleep(0.01)
        self._compact(vacuum and self.db_bytes() > 20 * MB)
        self._cache.pop("database", None)
        return {"freed_bytes": max(0, before - self.db_bytes()), "files": trimmed}

    def _compact(self, vacuum):
        with self.store.lock:
            if self.store.depth:
                return
            try:
                self.store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                if vacuum and time.time() - self.state.get("last_vacuum", 0) > 3 * 86400 \
                        and shutil.disk_usage(self.store.directory).free > 2 * self.db_bytes() + 100 * MB:
                    self.store.conn.execute("VACUUM")
                    self.state["last_vacuum"] = time.time()
                    self.save_state()
                    self.store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass  # sqlite3.Error or a closed store: compaction is best effort

    # ---- running --------------------------------------------------------------------------
    def run_auto(self):
        results = {}
        for category in AUTO:
            try:
                results[category] = self.clean(category)
            except Exception:
                results[category] = {"freed_bytes": 0, "files": 0}
        self.engine.files.purge_trash(7)  # agent-deleted files older than 7 days (never raises)
        return results

    def _record(self, results, manual=False):
        freed = sum(r["freed_bytes"] for r in results.values())
        files = sum(r["files"] for r in results.values())
        if freed > 0 or manual:
            self.state["last_cleanup"] = {"at": now(), "freed_bytes": freed, "files": files}
        self.save_state()
        self._cache.clear()
        return freed, files

    def cached(self, key, ttl, function):
        entry = self._cache.get(key)
        if entry and time.time() - entry[0] < ttl:
            return entry[1]
        value = function()
        self._cache[key] = (time.time(), value)
        return value

    def working(self):
        return self.thread is not None and self.thread.is_alive()

    def tick(self):
        """Engine heartbeat (every ~30 s). Cheap; the real work runs in a background thread."""
        try:
            tick = getattr(self.engine.providers, "background_tick", None)
            if tick:
                tick()
        except Exception:
            pass
        try:
            if time.monotonic() - self.started < self.startup_delay or self.working() or self.engine.stop.is_set():
                return
            current = time.time()
            due = current - self.state.get("last_run", 0) >= RUN_INTERVAL
            if not due and current - self.state.get("last_scan", 0) >= 1800:
                due = self.low_space(self.config())[0]
            if due:
                self.thread = threading.Thread(target=self.run_scheduled, daemon=True)
                self.thread.start()
        except Exception:
            pass

    def run_scheduled(self):
        if not self.work.acquire(blocking=False):
            return
        try:
            self.state["last_run"] = self.state["last_scan"] = time.time()
            self.save_state()
            if self.config()["auto_cleanup"]:
                self._record(self.run_auto())
            self.check_space()
        except Exception:
            pass
        finally:
            self.work.release()

    def low_space(self, cfg):
        usage = shutil.disk_usage(self.engine.workspace)
        return usage.free < cfg["min_free_gb"] * GB, usage

    def data_bytes(self):
        cached = self._cache.get("data")
        if cached and time.time() - cached[0] < 120:
            return cached[1]
        total = measure(self.store.directory)[0] + measure(self.engine.workspace)[0]
        total += sum(measure(p)[0] for root in self.artifact_roots() for p in self._entries(root) if RESET_NAME.match(p.name))
        self._cache["data"] = (time.time(), total)
        return total

    def ask_groups(self):
        groups = {}
        for category in ASK:
            items, _ = self.collect(category)
            if items:
                groups[category] = items
        return groups

    def check_space(self):
        """Ask the owner (once, non-blocking) when the drive is short on space or data is large."""
        cfg = self.config()
        low, usage = self.low_space(cfg)
        data = self.data_bytes()
        if not (low or data > DATA_LIMIT):
            return None
        if time.time() < self.state.get("snoozed_until", 0):
            return None
        if any(q["status"] == "open" and q.get("action_handler") == "cleanup" for q in self.store.all("decisions")):
            return None
        groups = self.ask_groups()
        total = sum(i["bytes"] for items in groups.values() for i in items)
        if total < MIN_ASK_BYTES:
            return None
        return self.ask_owner(groups, total, usage.free, data, low)

    def anchor_run(self):
        try:
            return self.store.get("runs", ANCHOR_RUN)
        except DomainError:
            return self.store.create("runs", {"id": ANCHOR_RUN, "agent_id": "a-lead", "prompt": "Disk housekeeping", "kind": "housekeeping",
                "status": "done", "steps": 0, "trace": [], "phase": "done", "pending_calls": [], "wait_for_runs": [],
                "wait_for_tasks": [], "wait_for_decisions": [], "root_id": ANCHOR_RUN, "channel": "dm-a-lead",
                "contract_version": 2, "retry_count": 0, "system_managed": True, "no_followup": True, "completed_at": now()})

    def ask_owner(self, groups, total, free, data, low):
        lines = []
        for category, items in groups.items():
            lines.append("- %s: %s (%d item%s)" % (LABELS[category][0], fmt(sum(i["bytes"] for i in items)), len(items), "" if len(items) == 1 else "s"))
        reason = "Only %s is free on the workspace drive." % fmt(free) if low else "AgentFlow data has grown to %s." % fmt(data)
        question = "%s I can free %s of old files. Clean up now?" % (reason, fmt(total))
        context = ("Nothing else is touched: results, registered documents, workspace files and attachments of active projects stay.\n"
                   "Files that would be deleted:\n" + "\n".join(lines))
        actor = self.store.get("agents", "a-lead")
        result = self.engine.team.ask(actor, {"question": question, "context": context, "blocking": False,
            "options": ["Free %s now" % fmt(total), "Not now"],
            "actions": [{"value": "clean", "label": "Free %s now" % fmt(total), "style": "primary", "hint": "Deletes only the files listed above."},
                        {"value": "skip", "label": "Not now", "style": "quiet", "hint": "Will not ask again for 24 hours."}],
            "action_field": "cleanup_action", "action_handler": "cleanup"}, self.anchor_run(), _routed=True)
        self.state["pending"] = {"decision_id": result["decision_id"], "created": time.time(),
            "items": [{k: i[k] for k in ("category", "path", "root", "bytes", "record")} for items in groups.values() for i in items]}
        self.save_state()
        return result

    def decision(self, question, answer):
        """decision_handlers['cleanup']: runs inside Collaboration.answer, so only schedule work here."""
        value = answer.get("cleanup_action")
        if value == "skip":
            self.state["snoozed_until"] = time.time() + ASK_SNOOZE
            self.state.pop("pending", None)
            self.save_state()
            return
        pending = self.state.get("pending") or {}
        items = pending.get("items") if pending.get("decision_id") == question.get("id") else None
        def work():
            try:
                self.perform_listed(items)
            except Exception:
                pass  # the store may be closing; the owner can retry from Settings
        threading.Thread(target=work, daemon=True).start()

    def perform_listed(self, items=None):
        with self.work:
            if items is None:  # state lost (restart): recompute exactly what would have been offered
                items = [i for group in self.ask_groups().values() for i in group]
            results = {}
            for item in items:
                results.setdefault(item["category"], []).append({**item, "record": tuple(item["record"]) if item.get("record") else None})
            summary = {c: self.clean_items(group) for c, group in results.items()}
            freed, files = self._record(summary, manual=True)
            self.state.pop("pending", None)
            self.save_state()
            try:
                self.engine.team.emit("a-lead", "Disk cleanup finished: freed %s (%d file%s)." % (fmt(freed), files, "" if files == 1 else "s"),
                                      self.anchor_run(), "chat")
            except DomainError:
                pass
            return summary

    def cleanup(self, categories=None):
        """POST /api/storage/cleanup: owner-initiated cleanup of the chosen categories."""
        known = AUTO + ASK
        if categories is None:
            categories = list(AUTO)
        if not isinstance(categories, list) or any(not isinstance(c, str) or c not in known for c in categories):
            raise DomainError("Choose valid storage categories.", 400)
        if not self.work.acquire(timeout=30):
            raise DomainError("A cleanup is already running. Try again in a moment.", 409)
        try:
            results = {c: self.clean(c) for c in dict.fromkeys(categories)}
            freed, files = self._record(results, manual=True)
        finally:
            self.work.release()
        return {"freed_bytes": freed, "files": files, "categories": [{"id": c, **r} for c, r in results.items()]}

    # ---- reporting ------------------------------------------------------------------------
    def report(self):
        cached = self._cache.get("report")
        if cached and time.time() - cached[0] < 20:
            return cached[1]
        cfg = self.config()
        low, usage = self.low_space(cfg)
        categories = []
        for category in AUTO + ASK:
            if category == "native_sessions":
                size, count = self.cached("sessions", 120, self._sessions_estimate)
                used = 0
            elif category == "database":
                size, count = self._database_estimate()
                used = self.db_bytes()
            else:
                items, used = self.cached("collect:" + category, 60, lambda c=category: self.collect(c))
                size, count = sum(i["bytes"] for i in items), sum(i["files"] for i in items)
            label, description = LABELS[category]
            categories.append({"id": category, "label": label, "description": description, "bytes": size, "files": count,
                               "auto": category in AUTO, "used_bytes": used})
        value = {"total_bytes": self.data_bytes(), "free_bytes": usage.free, "disk_total_bytes": usage.total, "categories": categories,
                 "last_cleanup": self.state.get("last_cleanup"), "settings": cfg, "low_space": low,
                 "snoozed_until": self.state.get("snoozed_until") or None}
        self._cache["report"] = (time.time(), value)
        return value
