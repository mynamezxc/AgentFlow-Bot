"""Project goals: one project can hold several goals. Agents add, update and complete them; only the owner removes."""
from .store import DomainError, now, uid

MAX_GOALS = 40
TITLE_LIMIT = 95


def norm(text):
    return " ".join(str(text or "").casefold().split())


def short_title(text):
    line = next((l for l in str(text or "").splitlines() if l.strip()), "")
    line = " ".join(line.split())
    if len(line) <= TITLE_LIMIT:
        return line
    cut = line[:TITLE_LIMIT - 1]
    if " " in cut[60:]:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,;:.-") + "…"


def compact(goal):
    return {k: goal.get(k) for k in ("id", "title", "text", "status")}


class Goals:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    @staticmethod
    def initial(title, brief, who="owner", status="active"):
        return [{"id": uid("g"), "title": str(title or "")[:250], "text": str(brief or title or "")[:30000], "status": status,
                 "created_at": now(), "created_by": who}]

    def migrate(self):
        """Every existing project's title/brief becomes its first goal (idempotent)."""
        for project in self.store.all("projects"):
            if not isinstance(project.get("goals"), list):
                done = project.get("status") == "done"
                self.store.patch("projects", project["id"], {"goals": self.initial(project.get("title"), project.get("brief"), "owner",
                                                                                   "done" if done else "active")})

    def listing(self, project):
        goals = project.get("goals")
        return goals if isinstance(goals, list) else self.initial(project.get("title"), project.get("brief"))

    def active(self, project):
        return [g for g in self.listing(project) if g.get("status") == "active"]

    def match(self, goals, text, title=""):
        keys = {k for k in (norm(text), norm(title)) if k}
        return next((g for g in goals if g.get("status") == "active" and keys & {norm(g.get("text")), norm(g.get("title"))}), None)

    def save(self, project, goals):
        return self.store.patch("projects", project["id"], {"goals": goals})

    def add(self, project_id, text, title="", who="owner"):
        text = str(text or "").strip() or str(title or "").strip()
        if not text or len(text) > 20000:
            raise DomainError("A goal needs 1 to 20,000 characters.")
        with self.store.transaction():
            project = self.store.get("projects", project_id)
            goals = list(self.listing(project))
            existing = self.match(goals, text, title)
            if existing:
                return {"goal": existing, "existing": True, "project": project, "goals": goals}
            if len([g for g in goals if g.get("status") == "active"]) >= MAX_GOALS:
                raise DomainError("This project has too many active goals. Complete one first.")
            goal = {"id": uid("g"), "title": (str(title or "").strip() or short_title(text))[:250], "text": text, "status": "active",
                    "created_at": now(), "created_by": who}
            goals.append(goal)
            return {"goal": goal, "existing": False, "project": self.save(project, goals), "goals": goals}

    def find(self, goals, goal_id):
        goal = next((g for g in goals if g.get("id") == goal_id), None)
        if not goal:
            raise DomainError("Goal not found. Use an id from the project goals list.", 404)
        return goal

    def update(self, project_id, goal_id, title=None, text=None):
        with self.store.transaction():
            project = self.store.get("projects", project_id)
            goals = [dict(g) for g in self.listing(project)]
            goal = self.find(goals, goal_id)
            if title is None and text is None:
                raise DomainError("Provide a new title or text.")
            if title is not None:
                goal["title"] = str(title).strip()[:250] or goal["title"]
            if text is not None:
                goal["text"] = str(text).strip()[:30000] or goal["text"]
            self.save(project, goals)
            return {"goal": goal, "goals": goals}

    def complete(self, project_id, goal_id, who="owner"):
        with self.store.transaction():
            project = self.store.get("projects", project_id)
            goals = [dict(g) for g in self.listing(project)]
            goal = self.find(goals, goal_id)
            goal.update(status="done", completed_at=now(), completed_by=who)
            self.save(project, goals)
            return {"goal": goal, "goals": goals}

    def remove(self, project_id, goal_id):
        """Owner only (service.action). Agents have no removal path."""
        with self.store.transaction():
            project = self.store.get("projects", project_id)
            goals = list(self.listing(project))
            goal = self.find(goals, goal_id)
            goals = [g for g in goals if g is not goal]
            self.save(project, goals)
            return {"removed": goal, "goals": goals}

    def invoke(self, args, actor, run):
        """Agent tool project.goals: add | update | complete. Never removes."""
        action = str(args.get("action", "")).strip()
        project_id = args.get("project_id") or run.get("project_id")
        if not project_id:
            raise DomainError("There is no current project for goals.")
        if action == "remove":
            raise DomainError("Only the owner removes goals. Complete a finished goal instead.", 403)
        if action == "add":
            result = self.add(project_id, args.get("text"), args.get("title", ""), actor["id"])
        elif action == "update":
            result = self.update(project_id, args.get("goal_id"), args.get("title"), args.get("text"))
        elif action == "complete":
            result = self.complete(project_id, args.get("goal_id"), actor["id"])
        else:
            raise DomainError("Use action add, update or complete.")
        answer = {"goal": compact(result["goal"]), "active_goals": [compact(g) for g in result["goals"] if g.get("status") == "active"]}
        if result.get("existing"):
            answer["note"] = "This goal is already active; keep working on it instead of adding a duplicate."
        return answer
