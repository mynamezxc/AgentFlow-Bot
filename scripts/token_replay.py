"""Replay stored tool results through the token-diet tool limits and print old vs new sizes.

Usage: python scripts/token_replay.py [DATA_DIR]

DATA_DIR holds workspace.sqlite3 (+ -wal/-shm). The database is copied to a temporary directory first and opened
read-only, so the live workspace is never modified. For every row in `tool_events` the stored result is passed through
the new capping logic (read chunks, search caps, web text normalisation, compact/diff aria snapshots, capped diffs,
compact agent/task listings). Where the original input is gone the replay is an approximation:

  * browser snapshots are re-compacted from the stored raw aria text; diff mode compares consecutive snapshots of the
    same URL in the same project browser session (browser.open never diffs; browser.close resets the baseline)
  * web.fetch only re-applies whitespace collapsing and the 10k cap (navigation stripping needs the HTML, so the
    real saving is slightly larger)
  * read outlines are built from the stored chunk, which holds at most the first 40k characters

Turn estimate: tool turns are grouped per run by `step`; see merged() for the conservative and optimistic rules
(consecutive read-only turns packed into batches of up to 8 calls, browser.act turns into actions:[...] of up to 10).
"""
import collections
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentflow.browser import (SNAPSHOT_CAP, SNAPSHOT_DIFF_CAP, aria_diff, aria_sections, cap_lines, compact_aria)  # noqa: E402
from agentflow.files import FileTools  # noqa: E402

CHARS_PER_TOKEN = 3.5
SEARCH_KEYS = ("path", "line", "text")
MECHANICAL = {"project.read", "workspace.read", "project.search", "workspace.search", "web.fetch", "browser.open", "browser.snapshot",
              "browser.act", "browser.screenshot", "outputs.inspect", "agents.inspect", "tasks.inspect", "tools.inspect", "memory.search",
              "skills.search", "outputs.view_image"}
OPTIMISTIC_EXTRA = {"progress.update", "team.wait"}


def size(value):
    return len(json.dumps(value, ensure_ascii=False))


def load(data_dir):
    source = Path(data_dir) / "workspace.sqlite3"
    if not source.is_file():
        raise SystemExit("No workspace.sqlite3 in " + str(data_dir))
    temp = Path(tempfile.mkdtemp(prefix="token-replay-"))
    for suffix in ("", "-wal", "-shm"):
        if Path(str(source) + suffix).exists():
            shutil.copyfile(str(source) + suffix, temp / ("workspace.sqlite3" + suffix))
    conn = sqlite3.connect(temp / "workspace.sqlite3")
    rows = [json.loads(p) for (p,) in conn.execute("SELECT payload FROM records WHERE kind='tool_events' ORDER BY rowid")]
    conn.close()
    shutil.rmtree(temp, ignore_errors=True)
    return rows


# ---- per-tool replay of the new limits -----------------------------------------------------------

def new_read(args, result):
    if isinstance(result, str):  # plain-text workspace.read of a small file
        result = {"content": result, "total_characters": len(result)}
    if "content" not in result:
        return result
    content, total = result["content"], result.get("total_characters") or len(result["content"])
    offset = int(args.get("offset") or 0)
    limit = min(FileTools.READ_MAX, max(100, int(args.get("limit") or FileTools.READ_DEFAULT)))
    out = {"path": result.get("path"), "content": content[:limit], "total_chars": total,
           "next_offset": offset + limit if total > offset + limit else None}
    if total > FileTools.READ_MAX and offset == 0:
        out["outline"] = FileTools.outline(content, limit)
    if total <= limit and not offset:
        return out["content"]  # small files stay plain text
    return out


def new_search(result):
    out, total = [], 0
    for match in result:
        text = str(match.get("text", "")).strip()
        if len(text) > FileTools.SEARCH_LINE:
            text = text[:FileTools.SEARCH_LINE - 1] + "…"
        row = {"path": match.get("path"), "line": match.get("line"), "text": text}
        weight = len(str(row["path"])) + len(text) + 24
        if len(out) >= FileTools.SEARCH_MAX_MATCHES or total + weight > FileTools.SEARCH_TOTAL:
            out.append({"note": "More matches exist; narrow the query or path."})
            break
        total += weight
        out.append(row)
    return out


def new_web(result):
    if "text" not in result:
        return result
    lines = (" ".join(line.split()) for line in str(result["text"]).splitlines())
    text = "\n".join(line for line in lines if line)
    return {"url": result.get("url"), "status": result.get("status"), "text": text[:10000], "total_chars": len(text),
            "next_offset": 10000 if len(text) > 10000 else None, "notice": result.get("notice")}


def new_inspect(result):
    if "before" not in result and "after" not in result and "diff" not in result:
        return result
    item = dict(result)
    if item.get("before") is None and item.get("diff"):  # rebuild missing sides from the stored line diff
        lines = item["diff"]
        item["before"] = "\n".join(d["text"] for d in lines if d["kind"] in ("context", "delete"))
        item["after"] = item.get("after") or "\n".join(d["text"] for d in lines if d["kind"] in ("context", "add"))
    return FileTools.change_view(FileTools, item)


def new_agents(args, result):
    if not isinstance(result, list):
        return result
    if not args.get("agent_id"):
        return [{**{k: a.get(k) for k in ("id", "name", "role", "category", "parent_id", "status", "provider", "model")},
                 "skills": len(a.get("skills") or [])} for a in result]
    out = []
    for a in result:
        a = dict(a)
        text = str(a.get("instructions") or "")
        if len(text) > 3000:
            a["instructions"] = text[:3000] + "…[truncated %d chars]" % (len(text) - 3000)
        out.append(a)
    return out


def new_tasks(args, result):
    if isinstance(result, dict) and "title" in result:
        task = dict(result)
        if len(str(task.get("result") or "")) > 8000:
            task["result"] = str(task["result"])[:8000] + "…[truncated]"
        return task
    if isinstance(result, list):
        return [{**{k: t.get(k) for k in ("id", "title", "status", "agent_id", "reviewer_id")},
                 "result": (str(t["result"])[:300] + ("…" if len(str(t["result"])) > 300 else "")) if t.get("result") else None} for t in result]
    return result


class SnapshotReplay:
    """Browser snapshots: compact + cap, diff against the previous snapshot of the same URL in the same project."""

    def __init__(self):
        self.last = {}

    def reset(self, project):
        self.last.pop(project, None)

    def apply(self, name, project, result):
        if not isinstance(result, dict) or "snapshot" not in result:
            return result
        lines = str(result["snapshot"]).splitlines()
        compact = compact_aria(lines)
        previous, url = self.last.get(project), result.get("url")
        self.last[project] = (url, compact)
        head = {k: result.get(k) for k in ("url", "title", "snapshot_id")}
        if name != "browser.open" and previous and previous[0] == url:
            if previous[1] == compact:
                return {**head, "unchanged": True, "total_lines": len(compact)}
            text, added, removed = aria_diff(previous[1], compact)
            if len(text) <= SNAPSHOT_DIFF_CAP:
                return {**head, "diff": text, "added": added, "removed": removed, "total_lines": len(compact), "note": "x" * 100}
        body, left = cap_lines(compact, SNAPSHOT_CAP, "call browser.snapshot {full:true, section:'<landmark name>'} to read the rest")
        out = {**head, "snapshot": body, "total_lines": len(compact), "note": "x" * 70}
        if left:
            out["sections"] = aria_sections(lines)
        return out


def replay(rows):
    """[(event, old_result, new_result)] in stored order."""
    snaps, out = SnapshotReplay(), []
    for event in rows:
        name, args, old = event.get("name"), event.get("arguments") or {}, event.get("result")
        args = args if isinstance(args, dict) else {}
        new = old
        try:
            if name in ("project.read", "workspace.read") and not (isinstance(old, dict) and "error" in old):
                new = new_read(args, old)
            elif name in ("project.search", "workspace.search") and isinstance(old, list):
                new = new_search(old)
            elif name == "web.fetch" and isinstance(old, dict):
                new = new_web(old)
            elif name in ("browser.open", "browser.snapshot", "browser.act"):
                new = snaps.apply(name, event.get("project_id"), old)
            elif name == "browser.close":
                snaps.reset(event.get("project_id"))
            elif name == "outputs.inspect" and isinstance(old, dict):
                new = new_inspect(old)
            elif name == "agents.inspect":
                new = new_agents(args, old)
            elif name == "tasks.inspect":
                new = new_tasks(args, old)
        except Exception as exc:  # a malformed historic row must not stop the report
            print("skipped", event.get("id"), name, type(exc).__name__, exc, file=sys.stderr)
        out.append((event, old, new))
    return out


# ---- turn estimate -------------------------------------------------------------------------------

GATHER = {"project.read", "workspace.read", "project.search", "workspace.search", "web.fetch", "outputs.inspect", "agents.inspect",
          "tasks.inspect", "tools.inspect", "memory.search", "skills.search"}


def merged(turns, optimistic):
    """Number of turns after batching. `turns` = [[tool names of one turn], ...] in order.

    conservative: consecutive turns made only of gather/read tools pack up to 8 calls; consecutive browser.act turns pack
    up to 10 actions (actions:[...]); a lone browser.screenshot turn right after a browser.open turn is folded into it.
    optimistic: consecutive turns made only of read-only/mechanical tools (browser steps included) pack up to 8 calls."""
    if not optimistic:
        folded = []
        for turn in turns:
            if turn == ["browser.screenshot"] and folded and "browser.open" in folded[-1]:
                continue
            folded.append(turn)
        turns = folded
    allowed = MECHANICAL | OPTIMISTIC_EXTRA

    def kind(turn):
        if optimistic:
            return "m" if all(n in allowed for n in turn) else None
        if all(n in GATHER for n in turn):
            return "gather"
        return "act" if all(n == "browser.act" for n in turn) else None

    total, i = 0, 0
    while i < len(turns):
        k = kind(turns[i])
        if k is None:
            total, i = total + 1, i + 1
            continue
        cap, calls, j = (10 if k == "act" else 8), len(turns[i]), i + 1
        while j < len(turns) and kind(turns[j]) == k and calls + len(turns[j]) <= cap:
            calls, j = calls + len(turns[j]), j + 1
        total, i = total + 1, j
    return total


def turn_estimate(rows):
    runs = collections.defaultdict(lambda: collections.defaultdict(list))
    for event in rows:
        runs[event.get("run_id")][int(event.get("step") or 0)].append(event.get("name"))
    old = cons = opt = 0
    for steps in runs.values():
        turns = [steps[k] for k in sorted(steps)]
        old += len(turns)
        cons += merged(turns, False)
        opt += merged(turns, True)
    return old, cons, opt


def context_growth(pairs):
    """Sum over tool turns of the tool-result characters already in context (what every later turn re-reads)."""
    runs = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0]))
    for event, old, new in pairs:
        cell = runs[event.get("run_id")][int(event.get("step") or 0)]
        cell[0] += size(old)
        cell[1] += size(new)
    old_total = new_total = 0
    for steps in runs.values():
        carried_old = carried_new = 0
        for key in sorted(steps):
            old_total += carried_old
            new_total += carried_new
            carried_old += steps[key][0]
            carried_new += steps[key][1]
    return old_total, new_total


# ---- report --------------------------------------------------------------------------------------

def table(headers, rows):
    widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
    line = lambda r: "  ".join(str(c).rjust(widths[i]) if i else str(c).ljust(widths[i]) for i, c in enumerate(r))
    return "\n".join([line(headers), "  ".join("-" * w for w in widths)] + [line(r) for r in rows])


def report(rows):
    pairs = replay(rows)
    per = collections.OrderedDict()
    for event, old, new in pairs:
        cell = per.setdefault(event.get("name"), [0, 0, 0])
        cell[0] += 1
        cell[1] += size(old)
        cell[2] += size(new)
    body = []
    for name, (calls, old, new) in sorted(per.items(), key=lambda kv: -kv[1][1]):
        body.append((name, calls, old, new, "%d%%" % (100 * (old - new) / old) if old else "-", int(old / calls), int(new / calls)))
    old_total, new_total = sum(c[1] for c in per.values()), sum(c[2] for c in per.values())
    print("TOOL RESULT CHARACTERS (stored result -> new limits)\n")
    print(table(("tool", "calls", "old chars", "new chars", "saved", "old avg", "new avg"), body))
    calls = sum(c[0] for c in per.values())
    print("\nTOTAL  %d calls   old %s chars (~%s tokens)   new %s chars (~%s tokens)   saved %d%%" % (
        calls, format(old_total, ","), format(int(old_total / CHARS_PER_TOKEN), ","), format(new_total, ","),
        format(int(new_total / CHARS_PER_TOKEN), ","), 100 * (old_total - new_total) / max(1, old_total)))
    grow_old, grow_new = context_growth(pairs)
    print("\nCONTEXT RE-READ PROXY (tool-result chars already in context, summed over later turns of each run)")
    print("  old %s   new %s   saved %d%%" % (format(grow_old, ","), format(grow_new, ","), 100 * (grow_old - grow_new) / max(1, grow_old)))
    turns_old, turns_cons, turns_opt = turn_estimate(rows)
    print("\nTOOL TURNS (distinct run steps with tool calls)")
    print(table(("", "turns", "saved"), [("recorded", turns_old, "-"),
          ("conservative batching", turns_cons, "%d%%" % (100 * (turns_old - turns_cons) / max(1, turns_old))),
          ("optimistic batching", turns_opt, "%d%%" % (100 * (turns_old - turns_opt) / max(1, turns_old)))]))


if __name__ == "__main__":
    report(load(sys.argv[1] if len(sys.argv) > 1 else "."))
