"""Editable work checklists; linked steps follow real task state."""
from .language import require_english
from .store import DomainError

STATES = {"todo", "doing", "done", "blocked", "skipped"}
TASK_STATES = {"backlog":"todo", "ready":"todo", "running":"doing", "review":"doing", "done":"done", "failed":"blocked", "cancelled":"skipped"}


class Progress:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def validate(self, data, previous=None):
        item = {**(previous or {}), **{k:v for k,v in data.items() if k in {
            "title", "note", "status", "agent_ids", "project_id", "task_id", "position", "archived"}}}
        self.store.get("projects", item.get("project_id", ""))
        title = str(item.get("title", "")).strip()
        if not title or len(title)>250 or len(str(item.get("note", "")))>4000:
            raise DomainError("Checklist step names must contain 1–250 characters, with notes up to 4,000 characters.")
        if item.get("status", "todo") not in STATES:
            raise DomainError("Invalid checklist status.")
        agents = item.get("agent_ids", [])
        if not isinstance(agents, list) or len(agents)>30:
            raise DomainError("Choose a valid list of task owners.")
        for agent_id in agents:
            self.store.get("agents", agent_id)
        if not isinstance(item.get("position", 0), (int,float)) or not -100000<=item.get("position",0)<=100000:
            raise DomainError("Invalid checklist position.")
        if item.get("task_id"):
            task = self.store.get("tasks", item["task_id"])
            if task.get("project_id") != item["project_id"]:
                raise DomainError("The checklist step and task must belong to the same project.")
        return {k:v for k,v in {"note":"", "status":"todo", "agent_ids":[], "position":0, **item, "title":title}.items()
                if k not in ("id", "version", "updated_at", "created_at")}

    def items(self, project_id):
        saved = [c for c in self.store.all("checklist") if c.get("project_id")==project_id]
        linked = {c.get("task_id") for c in saved}
        tasks = {t["id"]:t for t in self.store.all("tasks") if t.get("project_id")==project_id and not t.get("archived") and not t.get("sample")}
        result = []
        for item in saved:
            if item.get("archived"): continue
            task = tasks.get(item.get("task_id"))
            result.append({**item, **({"status":TASK_STATES.get(task["status"],"todo"), "task_status":task["status"]} if task else {})})
        for task in tasks.values():
            if task["id"] not in linked:
                result.append({"id":"task:"+task["id"], "task_id":task["id"], "project_id":project_id,
                    "title":task["title"], "note":task.get("review_note") or task.get("progress_note") or "",
                    "agent_ids":[task["agent_id"]], "status":TASK_STATES.get(task["status"],"todo"),
                    "task_status":task["status"], "position":len(result), "derived":True})
        return sorted(result, key=lambda c:c.get("position",0))

    def set(self, args, actor, run):
        project_id = run.get("project_id")
        if not project_id: raise DomainError("A current project is required for the checklist.")
        items = args.get("items", [])
        if not isinstance(items,list) or not 1<=len(items)<=40:
            raise DomainError("Update between 1 and 40 steps at a time.")
        results=[]
        with self.store.transaction():
            for index, incoming in enumerate(items):
                if not isinstance(incoming,dict): raise DomainError("Each step must be an object.")
                require_english("Checklist steps", incoming.get("title", ""), incoming.get("note", ""))
                previous = self.store.get("checklist",incoming["id"]) if incoming.get("id") else None
                if previous and previous.get("project_id")!=project_id:
                    raise DomainError("Only the current project checklist can be edited.",403)
                if not previous:
                    previous=next((c for c in self.store.all("checklist") if c.get("project_id")==project_id and c["title"]==incoming.get("title") and not c.get("archived")),None)
                defaults={} if previous else {"agent_ids":[actor["id"]], "position":index}
                document=self.validate({**defaults, **incoming, "project_id":project_id},previous)
                item=self.store.patch("checklist",previous["id"],document) if previous else self.store.create("checklist",document)
                results.append({"id":item["id"],"title":item["title"],"status":item["status"]})
        return {"items":results}
