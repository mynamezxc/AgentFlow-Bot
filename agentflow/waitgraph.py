"""Deadlock breaker for runs that wait on each other.

A run in `waiting_team` is woken only when everything it waits for (peer runs, tasks) is finished. When the wait graph
has a cycle (a reviewer waits for the task it must review, a worker waits for a peer that waits for the worker, ...)
or a dead end (a task in review that no review run can ever finish) nothing wakes anyone. This module finds those runs
on every sweep, resumes the one closest to the work with a clear reason, records one event and one activity note, and
after three repeats for the same root asks the owner what to do instead of resuming forever.
"""
import time
from datetime import datetime, timezone

from .contract import TERMINAL
from .store import DomainError, now

WAITING = "waiting_team"
LIMIT = 3           # automatic resumes per root before the owner is asked
GRACE = 30.0        # seconds a stuck run must have been quiet before the sweep touches it
INTERVAL = 2.0      # sweeps are throttled; unstick() and tests force one


def age_seconds(run):
    stamp = run.get("last_activity_at") or run.get("updated_at") or run.get("created_at")
    try:
        then = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return 1e9
    return (datetime.now(timezone.utc) - then).total_seconds()


def review_message(task):
    revision = int(task.get("revision") or 0)
    head = ("Revision %d of %s was resubmitted." % (revision, task["title"])) if revision else ("The draft of %s was submitted." % task["title"])
    return head + " Review it now with tasks.review (approve or revise). Do not call team.wait for this task: the runtime wakes you when it changes."


class WaitGraph:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self.interval, self.grace, self.limit = INTERVAL, GRACE, LIMIT
        self.last = 0.0
        self.counts = {}   # root id -> automatic resumes (kept on the root run when it exists)
        engine.decision_handlers["wait_cycle"] = self.decision

    # ---- analysis ------------------------------------------------------------------------------------------------------
    def analyze(self):
        """Waiting runs that can never be woken: (stuck runs by id, edges between them, runs, tasks)."""
        runs = {r["id"]: r for r in self.store.all("runs")}
        tasks = {t["id"]: t for t in self.store.all("tasks")}
        reviews = {}
        for run in runs.values():
            if run.get("review_task_id") and run["status"] not in TERMINAL:
                reviews.setdefault(run["review_task_id"], []).append(run["id"])
        busy = {r["agent_id"] for r in runs.values() if r["status"] not in TERMINAL and r["status"] != WAITING}
        waiting = [r for r in runs.values() if r["status"] == WAITING]
        ok = set()

        def run_ok(run_id):
            run = runs.get(run_id)
            return run is None or run["status"] in TERMINAL or run["status"] != WAITING or run_id in ok

        def task_ok(task_id):
            task = tasks.get(task_id)
            if task is None or task["status"] in TERMINAL or task.get("archived"):
                return True
            if task["status"] == "running":
                return run_ok(task.get("run_id"))
            if task["status"] == "review":
                found = reviews.get(task_id)
                if found:
                    return any(run_ok(i) for i in found)
                return bool(task.get("auto_review")) or task.get("reviewer_id") in busy   # dispatch or a busy reviewer can still review it
            return True   # ready / backlog / blocked wait for the owner or the dispatcher, which is not a deadlock

        changed = True
        while changed:
            changed = False
            for run in waiting:
                if run["id"] not in ok and all(run_ok(i) for i in run.get("wait_for_runs", [])) and all(task_ok(i) for i in run.get("wait_for_tasks", [])):
                    ok.add(run["id"])
                    changed = True
        stuck = {r["id"]: r for r in waiting if r["id"] not in ok}
        edges = {rid: set() for rid in stuck}
        for rid, run in stuck.items():
            for peer in run.get("wait_for_runs", []):
                if peer in stuck:
                    edges[rid].add(peer)
            for task_id in run.get("wait_for_tasks", []):
                task = tasks.get(task_id) or {}
                if task.get("status") == "running" and task.get("run_id") in stuck:
                    edges[rid].add(task["run_id"])
                elif task.get("status") == "review":
                    edges[rid].update(i for i in reviews.get(task_id, []) if i in stuck)
        return stuck, edges, runs, tasks

    @staticmethod
    def components(edges):
        """Groups of stuck runs that wait on each other (a run that reaches itself). Returns a list of sets."""
        reach = {}
        for start in edges:
            seen, todo = set(), list(edges[start])
            while todo:
                node = todo.pop()
                if node in seen:
                    continue
                seen.add(node)
                todo.extend(edges.get(node, ()))
            reach[start] = seen
        groups, used = [], set()
        for node in edges:
            if node in used or node not in reach[node]:
                continue
            group = {n for n in reach[node] if node in reach.get(n, ())} | {node}
            used |= group
            groups.append(group)
        return groups

    # ---- naming --------------------------------------------------------------------------------------------------------
    def describe(self, run, tasks):
        try:
            name = self.store.get("agents", run["agent_id"])["name"]
        except DomainError:
            name = run["agent_id"]
        task = tasks.get(run.get("review_task_id") or run.get("task_id") or "")
        if run.get("review_task_id") and task:
            return "%s (review of '%s')" % (name, task["title"][:60])
        if task:
            return "%s (task '%s')" % (name, task["title"][:60])
        return "%s (%s)" % (name, "peer request" if run.get("kind") == "peer" else "chat" if run.get("kind") == "chat" else run.get("kind") or "work")

    @staticmethod
    def depth(run, runs):
        steps, seen = 0, set()
        while run and run.get("parent_run_id") in runs and run["id"] not in seen:
            seen.add(run["id"])
            run = runs[run["parent_run_id"]]
            steps += 1
        return steps

    def pick(self, group, runs, tasks):
        """The run closest to the work: a reviewer's review run whose task awaits review, else the deepest waiter."""
        def rank(run_id):
            run = runs[run_id]
            task = tasks.get(run.get("review_task_id") or "")
            return (1 if task and task["status"] == "review" else 0, self.depth(run, runs), run.get("created_at", ""))
        return runs[max(group, key=rank)]

    # ---- resuming ------------------------------------------------------------------------------------------------------
    def resume(self, run, reason):
        task = None
        if run.get("review_task_id"):
            try:
                task = self.store.get("tasks", run["review_task_id"])
            except DomainError:
                task = None
        if task and task["status"] == "review":
            reason = review_message(task) + "\n" + reason
        self.store.patch("runs", run["id"], {"status": "queued", "wait_for_runs": [], "wait_for_tasks": [], "wait_reason": "", "resume_reason": reason[:3000]})
        if task and task["status"] == "review":
            self.store.patch("tasks", task["id"], {"review_run_id": run["id"]})
        return run["id"]

    def resume_review(self, run, task):
        """Engine.review(): a review run that is still waiting from an earlier round picks the resubmitted draft up now."""
        self.store.patch("runs", run["id"], {"status": "queued", "wait_for_runs": [], "wait_for_tasks": [], "wait_reason": "",
                                             "resume_reason": review_message(task)})
        self.store.patch("tasks", task["id"], {"review_run_id": run["id"]})
        return self.store.get("runs", run["id"])

    def root_of(self, run):
        return run.get("root_id") or run["id"]

    def breaks(self, root_id):
        try:
            return int(self.store.get("runs", root_id).get("wait_cycle_breaks") or 0)
        except DomainError:
            return self.counts.get(root_id, 0)

    def count(self, root_id, value):
        try:
            self.store.patch("runs", root_id, {"wait_cycle_breaks": value})
        except DomainError:
            self.counts[root_id] = value

    # ---- sweep ---------------------------------------------------------------------------------------------------------
    def sweep(self, root_id=None, manual=False, force=False):
        """Resume one run per wait cycle / dead wait. `manual` (the owner's Resume button) ignores the grace period and the
        repeat limit. Returns the resumed run ids."""
        if not (manual or force) and time.monotonic() - self.last < self.interval:
            return []
        self.last = time.monotonic()
        stuck, edges, runs, tasks = self.analyze()
        fallback = False
        if root_id:
            stuck = {k: v for k, v in stuck.items() if self.root_of(v) == root_id}
            edges = {k: {p for p in v if p in stuck} for k, v in edges.items() if k in stuck}
            if not stuck:
                live = [r for r in runs.values() if self.root_of(r) == root_id and r["status"] not in TERMINAL]
                if live and all(r["status"] == WAITING for r in live):   # the owner sees "stuck": believe them
                    stuck = {r["id"]: r for r in live}
                    edges = {k: set() for k in stuck}
                    fallback = True
        if not stuck:
            return []
        groups = [set(stuck)] if fallback else self.components(edges)
        covered = set().union(*groups) if groups else set()
        dead = [] if fallback else [{rid} for rid in stuck if rid not in covered and not edges[rid]]   # waits that end nowhere
        resumed, by_root = [], {}
        for group in groups + dead:
            members = [stuck[i] for i in group]
            if any(self.engine.safety.halted(r) for r in members):
                continue
            if not manual and any(age_seconds(r) < self.grace for r in members):
                continue
            run = self.pick(group, runs, tasks)
            root = self.root_of(run)
            if not manual and self.breaks(root) >= self.limit:
                self.ask_owner(root, members, tasks)
                continue
            names = ", ".join(self.describe(r, tasks) for r in members)
            reason = ("Wait cycle detected: %s were waiting on each other, so nothing could finish. Do the next concrete step now with what "
                      "you have instead of waiting again." % names)
            by_root.setdefault(root, []).append((self.resume(run, reason), names))
            resumed.append(run["id"])
        for root, items in by_root.items():
            self.count(root, self.breaks(root) + 1)
            detail = "; ".join("resumed %s" % i[0] + " (cycle: %s)" % i[1] for i in items)
            self.store.event("Wait cycle resolved", detail[:1500], "task")
            self.note(root, runs, "Teammates were waiting on each other. I resumed " + ", ".join(
                self.describe(runs[i[0]], tasks) for i in items) + " so the work can continue.")
        return resumed

    def note(self, root_id, runs, text):
        root = runs.get(root_id)
        try:
            self.store.create("messages", {"agent_id": (root or {}).get("agent_id", "a-lead"), "role": "assistant", "content": text,
                "channel": (root or {}).get("owner_channel") or (root or {}).get("channel") or "dm-a-lead", "project_id": (root or {}).get("project_id"),
                "run_id": root_id if root else None, "kind": "progress", "audience": "activity"})
        except DomainError:
            pass

    # ---- the owner decides after repeated cycles -------------------------------------------------------------------------------
    def ask_owner(self, root_id, members, tasks):
        existing = [d for d in self.store.all("decisions") if d.get("action_handler") == "wait_cycle" and d["status"] == "open"
                    and (d.get("wait_cycle") or {}).get("root_id") == root_id]
        if existing:
            return existing[0]["id"]
        run = next((r for r in members if self.root_of(r) == root_id and r["id"] == root_id), members[0])
        names = ", ".join(self.describe(r, tasks) for r in members)
        asked = self.engine.team.ask(self.store.get("agents", "a-lead"), {"question": "These teammates keep waiting on each other: %s. Resume them again or cancel this branch?" % names,
            "context": "AgentFlow already resumed this work %d times and it got stuck again. Resume tries once more; Cancel branch stops these runs." % self.limit,
            "blocking": False, "options": ["Resume", "Cancel branch"],
            "actions": [{"value": "resume", "label": "Resume", "style": "primary"}, {"value": "cancel", "label": "Cancel branch", "style": "quiet"}],
            "action_field": "wait_cycle_action", "action_handler": "wait_cycle"}, run, _routed=True)
        self.store.patch("decisions", asked["decision_id"], {"wait_cycle": {"root_id": root_id, "run_ids": [r["id"] for r in members]}})
        self.store.event("Wait cycle needs a decision", names[:500], "task")
        return asked["decision_id"]

    def decision(self, question, answer):
        """decision_handlers["wait_cycle"]: runs inside Collaboration.answer."""
        info = question.get("wait_cycle") or {}
        if answer.get("wait_cycle_action") == "resume":
            self.count(info.get("root_id"), 0)
            self.sweep(root_id=info.get("root_id"), manual=True)
            return
        for run_id in info.get("run_ids", []):
            try:
                if self.store.get("runs", run_id)["status"] not in TERMINAL:
                    self.engine.cancel(run_id)
            except DomainError:
                continue
