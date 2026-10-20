"""Agent-authored broker recipes. No eval, host code or new privileges."""
import json
import os
import re
from pathlib import Path

from .store import DomainError, now
from .language import english_authoring

SAFE_STEPS = {"workspace.read", "workspace.write", "workspace.search", "memory.search", "memory.write",
              "web.fetch", "content.draft", "image.generate", "plugins.invoke"}


# Script tools (contract 4): code under the company workspace tools/<slug>/, run through the sandboxed
# workspace.execute path (same approvals), arguments as JSON on stdin and as `--args <json>`.
LANGUAGES = {"python": ".py", "node": ".js", "powershell": ".ps1"}
MAX_FILES, MAX_BYTES, MAX_ARGS_BYTES = 20, 200000, 100000
ARG_LIMIT = 4000       # larger payloads pass `--args -` and are read from stdin only
STDOUT_CAP, STDERR_CAP = 16000, 4000   # together <= 20 KB returned to the model
NAME = r"[a-z][a-z0-9_-]{0,59}"


def agent_owned(tool):
    """Only tools authored by an agent can be changed by agents; owner-provided and built-in tools are read only."""
    return (tool.get("created_by") or tool.get("authored_by") or "") not in ("", "owner", "builtin", "system")


def parse_json(text):
    try:
        value = json.loads(str(text).strip())
    except ValueError:
        return None
    return value if isinstance(value, (dict, list)) else None


class CustomTools:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def list(self):
        base = ("id", "name", "description", "inputs", "steps", "authored_by", "version")
        extra = ("kind", "language", "entry", "created_by")
        return [{**{k: t.get(k) for k in base}, **{k: t[k] for k in extra if k in t}}
                for t in self.store.all("custom_tools") if not t.get("archived")]

    def archive(self, args, actor=None):
        archived=args.get("archived",True)
        if not isinstance(archived,bool):
            raise DomainError("archived must be true or false.")
        tool=self.store.get("custom_tools",args.get("tool_id",""))
        if actor is not None and not agent_owned(tool):
            raise DomainError("Only tools created by agents can be archived by agents.",403)
        item=self.store.patch("custom_tools",tool["id"],{"archived":archived})
        return {"tool_id":item["id"],"archived":archived,"version":item["version"]}

    # ---- script tools --------------------------------------------------------------------
    def _rel(self, slug, path):
        """Workspace-relative path of a tool file. Accepts `main.py` or `tools/<slug>/main.py`."""
        text = str(path or "").replace("\\", "/").strip()
        prefix = "tools/" + slug + "/"
        text = text[len(prefix):] if text.startswith(prefix) else text
        parts = Path(text).parts
        if not text or text.startswith("/") or ":" in text or not parts or ".." in parts or "." in parts:
            raise DomainError("Tool files must be relative to tools/" + slug + "/.")
        return prefix + "/".join(parts)

    def _current_files(self, tool):
        files = {}
        for item in tool.get("files", []):
            path = self.engine.files.path(item["path"])
            if path.is_file():
                files[item["path"]] = path.read_text(encoding="utf-8", errors="replace")
        return files

    def _check_script(self, name, language, entry, files, inputs, test, timeout):
        if language not in LANGUAGES:
            raise DomainError("language must be python, node or powershell.")
        if not isinstance(files, dict) or not files or len(files) > MAX_FILES or sum(len(c.encode()) for c in files.values()) > MAX_BYTES:
            raise DomainError("Script tools need 1 to %d files and at most %d KB in total." % (MAX_FILES, MAX_BYTES // 1000))
        for path in files:
            self.engine.files.path(path)  # extension, hidden-name, secret-name and symlink checks
        if entry not in files or Path(entry).suffix.casefold() != LANGUAGES[language]:
            raise DomainError("entry must be one of the files and use the " + LANGUAGES[language] + " extension for " + language + ".")
        if not isinstance(inputs, dict) or len(inputs) > 20 or any(not re.fullmatch(r"[a-z][a-z0-9_]{0,59}", k) or not isinstance(v, str) for k, v in inputs.items()):
            raise DomainError("inputs maps lowercase names to English documentation strings (max 20).")
        if test is not None:
            if (not isinstance(test, dict) or set(test) - {"arguments", "expect"} or not isinstance(test.get("arguments", {}), dict)
                    or not isinstance(test.get("expect", ""), (str, dict)) or len(json.dumps(test)) > 20000):
                raise DomainError("test is {arguments:{...}, expect:'substring or JSON key'}.")
            if set(test.get("arguments", {})) - set(inputs):
                raise DomainError("test.arguments may only use declared inputs.")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 5 <= timeout <= 300:
            raise DomainError("timeout must be a whole number of seconds from 5 to 300.")

    def _write_files(self, actor, run, files):
        run = run or {"id": "manual", "project_id": None}
        for path, content in files.items():
            self.engine.files.write({"path": path, "content": content, "title": Path(path).name}, actor, run)

    def create_script(self, actor, args, run):
        name, description = str(args.get("name", "")).strip(), str(args.get("description", "")).strip()
        language, inputs = str(args.get("language", "python")), args.get("inputs", {})
        english_authoring(name, description, *(inputs.values() if isinstance(inputs, dict) else []))
        if not re.fullmatch(NAME, name) or not description or len(description) > 3000:
            raise DomainError("Tools require a lowercase name and an English description.")
        if next((t for t in self.store.all("custom_tools") if t["name"] == name and not t.get("archived")), None):
            raise DomainError("A tool named " + name + " exists. Use tools.update with its tool_id to change it.", 409)
        raw = args.get("files")
        if not isinstance(raw, dict):
            raise DomainError("files must map relative paths to file content.")
        files = {self._rel(name, p): c for p, c in raw.items() if isinstance(c, str)}
        if len(files) != len(raw):
            raise DomainError("Every file needs text content.")
        default = "main" + LANGUAGES.get(language, ".py")
        entry = self._rel(name, args.get("entry") or default)
        test, timeout = args.get("test"), args.get("timeout", 60)
        self._check_script(name, language, entry, files, inputs, test, timeout)
        self._write_files(actor, run, files)
        item = self.store.create("custom_tools", {"name": name, "description": description, "kind": "script", "language": language,
            "entry": entry, "dir": "tools/" + name, "files": [{"path": p, "bytes": len(c.encode())} for p, c in sorted(files.items())],
            "inputs": inputs, "steps": [], "test": test, "timeout": timeout, "created_by": actor["id"], "authored_by": actor["id"]})
        return {"id": item["id"], "name": name, "version": item["version"], "dir": item["dir"], "entry": entry,
                "next_step": "Run tools.test with this tool_id and fix failures with tools.update."}

    def update(self, actor, args, run):
        tool = self.store.get("custom_tools", args.get("tool_id", ""))
        if tool.get("archived"):
            raise DomainError("This tool is archived. Restore it with tools.archive archived=false first.", 409)
        if not agent_owned(tool):
            raise DomainError("Only tools created by agents can be changed by agents. Owner-provided and built-in tools are read only.", 403)
        if tool.get("kind") != "script":
            merged = {"name": tool["name"], "description": args.get("description", tool["description"]),
                      "inputs": args.get("inputs", tool.get("inputs", {})), "steps": args.get("steps", tool.get("steps"))}
            return self.save(actor, merged, run)
        name = tool["name"]
        description = str(args.get("description", tool["description"])).strip()
        inputs, test = args.get("inputs", tool.get("inputs", {})), args.get("test", tool.get("test"))
        timeout = args.get("timeout", tool.get("timeout", 60))
        english_authoring(description, *(inputs.values() if isinstance(inputs, dict) else []))
        if not description or len(description) > 3000:
            raise DomainError("Provide an English description of at most 3000 characters.")
        current = self._current_files(tool)
        changes = args.get("files", {})
        if not isinstance(changes, dict):
            raise DomainError("files maps relative paths to new content, or null to remove a file.")
        merged = dict(current)
        written = {}
        for path, content in changes.items():
            rel = self._rel(name, path)
            if content is None:
                merged.pop(rel, None)
            elif isinstance(content, str):
                merged[rel] = written[rel] = content
            else:
                raise DomainError("File content must be text, or null to remove the file.")
        entry = self._rel(name, args["entry"]) if args.get("entry") else tool["entry"]
        language = tool["language"]
        self._check_script(name, language, entry, merged, inputs, test, timeout)
        snapshot = {"version": tool["version"], "description": tool["description"], "entry": tool["entry"], "inputs": tool.get("inputs", {}),
                    "test": tool.get("test"), "files": current, "saved_at": now()}
        self._write_files(actor, run, written)
        for rel in set(current) - set(merged):
            path = self.engine.files.path(rel)
            if path.is_file() and not path.is_symlink():
                path.unlink()
        item = self.store.patch("custom_tools", tool["id"], {"description": description, "entry": entry, "inputs": inputs, "test": test,
            "timeout": timeout, "files": [{"path": p, "bytes": len(c.encode())} for p, c in sorted(merged.items())],
            "previous_version": snapshot, "last_test": None, "authored_by": actor["id"]})
        return {"id": item["id"], "name": name, "version": item["version"], "files": [f["path"] for f in item["files"]],
                "next_step": "Run tools.test to verify the change."}

    def command(self, tool, payload):
        entry = str(Path(tool["entry"]).relative_to(tool["dir"]).as_posix())
        flag = payload if len(payload) <= ARG_LIMIT else "-"
        language = tool["language"]
        if language == "python":
            return ["python" if os.name == "nt" else "python3", entry, "--args", flag]
        if language == "node":
            return ["node", entry, "--args", flag]
        return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", entry, "--args", flag]

    def run_script(self, tool, params, actor, run):
        payload = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode()) > MAX_ARGS_BYTES:
            raise DomainError("Tool arguments exceed 100 KB.")
        raw = self.engine.commands.execute({"command": self.command(tool, payload), "cwd": tool["dir"], "timeout": tool.get("timeout", 60),
                                            "stdin": payload}, actor, run, scope="company")
        if not isinstance(raw, dict) or not raw.get("executed"):
            return {**(raw if isinstance(raw, dict) else {}), "tool_id": tool["id"]}
        stdout, stderr = str(raw.get("stdout", "")), str(raw.get("stderr", ""))
        result = {"tool_id": tool["id"], "name": tool["name"], "version": tool["version"], "executed": True, "exit_code": raw.get("exit_code"),
                  "stdout": stdout[-STDOUT_CAP:], "stderr": stderr[-STDERR_CAP:],
                  "truncated": len(stdout) > STDOUT_CAP or len(stderr) > STDERR_CAP}
        parsed = parse_json(stdout)
        if parsed is not None and not result["truncated"]:
            result["json"] = parsed
        return result

    def test(self, actor, args, run):
        tool = self.store.get("custom_tools", args.get("tool_id", ""))
        if tool.get("archived") or tool.get("kind") != "script":
            raise DomainError("tools.test runs the test case of an active script tool.")
        case = tool.get("test") or {}
        params = args.get("arguments", case.get("arguments", {}))
        if not isinstance(params, dict) or set(params) - set(tool.get("inputs", {})):
            raise DomainError("Test arguments must use declared inputs only.")
        result = self.run_script(tool, params, actor, run)
        if not result.get("executed"):
            return result  # waiting for the owner's command approval, or cancelled
        passed, reason = self.check(case.get("expect"), result["exit_code"], result["stdout"])
        self.store.patch("custom_tools", tool["id"], {"last_test": {"passed": passed, "at": now(), "exit_code": result["exit_code"], "reason": reason}})
        return {**result, "passed": passed, "reason": reason, "expect": case.get("expect")}

    @staticmethod
    def check(expect, exit_code, stdout):
        if exit_code != 0:
            return False, "Exit code " + str(exit_code) + "; the script must exit with 0."
        if expect in (None, "", {}):
            return True, "Exit code 0."
        parsed = parse_json(stdout)
        if isinstance(expect, str):
            if expect in stdout:
                return True, "Output contains the expected text."
            if isinstance(parsed, dict) and expect in parsed:
                return True, "Output JSON has the expected key."
            return False, "Output contains neither the expected text nor a JSON key named " + json.dumps(expect) + "."
        if isinstance(parsed, dict) and all(k in parsed and parsed[k] == v for k, v in expect.items()):
            return True, "Output JSON matches the expected values."
        return False, "Output is not a JSON object matching the expected values."

    def save(self, actor, args, run=None):
        if args.get("kind") == "script":
            return self.create_script(actor, args, run)
        if args.get("kind") not in (None, "", "recipe"):
            raise DomainError("kind is script or recipe (omit it for step recipes).")
        name, description = str(args.get("name", "")).strip(), str(args.get("description", "")).strip()
        steps, inputs = args.get("steps"), args.get("inputs", {})
        english_authoring(name,description,*inputs.values() if isinstance(inputs,dict) else [])
        if (not re.fullmatch(r"[a-z][a-z0-9_-]{0,59}", name) or not description or len(description)>3000
            or not isinstance(steps, list) or not 1<=len(steps)<=12 or not isinstance(inputs, dict)
            or len(inputs)>20 or any(not re.fullmatch(r"[a-z][a-z0-9_]{0,59}", k) or not isinstance(v,str) for k,v in inputs.items())
            or len(json.dumps(args))>40000):
            raise DomainError("Tools require a lowercase name, description, inputs and 1–12 broker steps.")
        def validate_refs(value, index):
            if isinstance(value, dict):
                if "$input" in value:
                    if set(value)!={"$input"} or value["$input"] not in inputs:
                        raise DomainError("This input reference was not declared.")
                elif "$step" in value:
                    if set(value)-{"$step", "path"} or not isinstance(value["$step"], int) or not 0<=value["$step"]<index or not isinstance(value.get("path",[]),list):
                        raise DomainError("Only earlier step results can be referenced.")
                else:
                    for nested in value.values(): validate_refs(nested, index)
            elif isinstance(value, list):
                for nested in value: validate_refs(nested, index)
        for index, step in enumerate(steps):
            if not isinstance(step,dict) or set(step)!={"name","arguments"} or step["name"] not in SAFE_STEPS or not isinstance(step["arguments"],dict):
                raise DomainError("Composite tools may use broker data operations only. They cannot install host code or expand permissions.")
            validate_refs(step["arguments"], index)
        existing = next((t for t in self.store.all("custom_tools") if t["name"]==name and not t.get("archived")),None)
        document = {"name":name, "description":description, "inputs":inputs, "steps":steps, "authored_by":actor["id"]}
        if existing:
            document["previous_version"] = {k:existing[k] for k in ("inputs","steps","version")}
            item = self.store.patch("custom_tools",existing["id"],document)
        else:
            item = self.store.create("custom_tools",document)
        return {k:item[k] for k in ("id","name","version")}

    def invoke(self, actor, args, run):
        tool = self.store.get("custom_tools",args.get("tool_id", ""))
        params = args.get("arguments",{})
        if tool.get("kind") == "script":
            if tool.get("archived") or not isinstance(params, dict) or set(params) - set(tool.get("inputs", {})):
                raise DomainError("Provide only declared tool inputs.")
            return self.run_script(tool, params, actor, run)
        if tool.get("archived") or not isinstance(params,dict) or set(params)!=set(tool["inputs"]):
            raise DomainError("Provide all declared tool inputs.")
        results=[]
        def resolve(value):
            if isinstance(value,dict):
                if "$input" in value: return params[value["$input"]]
                if "$step" in value:
                    result=results[value["$step"]]
                    try:
                        for key in value.get("path",[]): result=result[key]
                    except (KeyError, IndexError, TypeError):
                        raise DomainError("The earlier result does not contain the declared field.") from None
                    return result
                return {k:resolve(v) for k,v in value.items()}
            if isinstance(value,list): return [resolve(v) for v in value]
            return value
        for step in tool["steps"]:
            if step["name"] not in SAFE_STEPS:
                raise DomainError("This composite tool contains a prohibited step.",403)
            result=self.engine.tool(actor,{"name":step["name"],"arguments":resolve(step["arguments"])},run)
            results.append(result)
            if isinstance(result,dict) and result.get("waiting_permission"):
                return {**result,"completed_steps":len(results)-1}
        return {"tool_id":tool["id"],"version":tool["version"],"results":results}
