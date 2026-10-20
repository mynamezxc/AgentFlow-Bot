import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import test_runtime as runtime
from agentflow.store import DomainError
from server import make_handler


def digest(data):
    return hashlib.sha256(data).hexdigest()


class FileSaveBase(unittest.TestCase):
    def setUp(self):
        runtime.RuntimeTests.setUp(self)
        self.run = self.engine.chat({"agent_id": "a-lead", "message": "Write the notes"})
        self.actor = self.store.get("agents", "a-lead")

    tearDown = runtime.RuntimeTests.tearDown

    def make(self, name, data):
        path = self.engine.workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return self.engine.outputs.register({"path": name, "title": name}, self.actor, self.run)

    def changes(self):
        return [c for c in self.store.all("file_changes") if c["agent_id"] == "owner"]


class SaveTests(FileSaveBase):
    def test_preview_reports_hash_and_editability(self):
        item = self.make("notes.md", "a\nb\n")
        preview = self.engine.outputs.preview("artifact", item["id"])
        self.assertEqual(preview["sha256"], digest(b"a\nb\n"))
        self.assertTrue(preview["editable"])
        self.assertEqual(preview["readonly_reason"], "")
        self.assertTrue(preview["can_open"])
        self.make("shot.png", b"\x89PNG\r\n\x1a\nfixture")
        image = self.engine.outputs.preview("artifact", next(a for a in self.store.all("artifacts") if a["path"] == "shot.png")["id"])
        self.assertFalse(image["editable"])
        self.assertTrue(image["sha256"])

    def test_save_writes_atomically_updates_the_artifact_and_records_the_change(self):
        item = self.make("notes.md", "a\nb\n")
        before = self.engine.outputs.preview("artifact", item["id"])["sha256"]
        result = self.engine.outputs.save("artifact", item["id"], {"content": "a\nB\nc\n", "base_sha256": before})
        path = self.engine.workspace / "notes.md"
        self.assertEqual(path.read_bytes(), b"a\nB\nc\n")
        self.assertEqual(result["sha256"], digest(b"a\nB\nc\n"))
        self.assertEqual(result["content"], "a\nB\nc\n")
        self.assertFalse(list(path.parent.glob("*.agentflow-tmp")))
        artifact = self.store.get("artifacts", item["id"])
        self.assertEqual((artifact["sha256"], artifact["bytes"]), (digest(b"a\nB\nc\n"), 6))
        change = self.changes()[0]
        self.assertEqual((change["id"], change["artifact_id"], change["kind"], change["added"], change["deleted"]),
                         (result["change_id"], item["id"], "modified", 2, 1))
        self.assertEqual((change["before"], change["after"]), ("a\nb\n", "a\nB\nc\n"))
        view = self.engine.files.change(change["id"])
        self.assertTrue(any(line["kind"] == "add" and line["text"] == "c" for line in view["diff"]))
        note = [m for m in self.store.all("messages") if m.get("audience") == "activity" and m["content"] == "Owner edited notes.md"]
        self.assertEqual(len(note), 1)
        self.assertEqual(note[0]["project_id"], self.run["project_id"])
        # the registered hash follows the save, so the file stays previewable and a second save works from the new hash
        self.engine.outputs.save("artifact", item["id"], {"content": "final", "base_sha256": result["sha256"]})
        self.assertEqual(len(self.changes()), 2)

    def test_unchanged_content_records_nothing(self):
        item = self.make("same.md", "x\n")
        base = digest(b"x\n")
        result = self.engine.outputs.save("artifact", item["id"], {"content": "x\n", "base_sha256": base})
        self.assertIsNone(result["change_id"])
        self.assertEqual(self.changes(), [])

    def test_conflicting_hash_is_a_409_with_the_current_hash(self):
        item = self.make("notes.md", "a\n")
        stale = digest(b"a\n")
        (self.engine.workspace / "notes.md").write_bytes(b"changed elsewhere\n")
        with self.assertRaises(DomainError) as caught:
            self.engine.outputs.save("artifact", item["id"], {"content": "mine\n", "base_sha256": stale})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.extra, {"current_sha256": digest(b"changed elsewhere\n")})
        self.assertEqual((self.engine.workspace / "notes.md").read_bytes(), b"changed elsewhere\n")
        with self.assertRaises(DomainError) as missing:
            self.engine.outputs.save("artifact", item["id"], {"content": "mine\n"})
        self.assertEqual(missing.exception.status, 409)
        self.assertEqual(self.changes(), [])

    def test_attachments_are_read_only(self):
        with self.assertRaises(DomainError) as caught:
            self.engine.outputs.save("attachment", "anything", {"content": "x", "base_sha256": "y"})
        self.assertEqual(caught.exception.status, 403)
        self.assertIn("read-only", str(caught.exception))

    def test_line_endings_and_bom_are_preserved(self):
        crlf = self.make("win.txt", b"one\r\ntwo\r\n")
        self.engine.outputs.save("artifact", crlf["id"], {"content": "one\ntwo\nthree\n", "base_sha256": digest(b"one\r\ntwo\r\n")})
        self.assertEqual((self.engine.workspace / "win.txt").read_bytes(), b"one\r\ntwo\r\nthree\r\n")
        lf = self.make("unix.txt", b"one\ntwo\n")
        self.engine.outputs.save("artifact", lf["id"], {"content": "one\r\nuno\r\n", "base_sha256": digest(b"one\ntwo\n")})
        self.assertEqual((self.engine.workspace / "unix.txt").read_bytes(), b"one\nuno\n")
        bom = self.make("bom.md", b"\xef\xbb\xbfhello\n")
        self.engine.outputs.save("artifact", bom["id"], {"content": "hello there\n", "base_sha256": digest(b"\xef\xbb\xbfhello\n")})
        self.assertEqual((self.engine.workspace / "bom.md").read_bytes(), b"\xef\xbb\xbfhello there\n")

    def test_binary_and_non_utf8_files_are_refused(self):
        binary = self.make("data.json", b"\xff\xfe\x00\x01binary")
        preview = self.engine.outputs.preview("artifact", binary["id"])
        self.assertFalse(preview["editable"])
        with self.assertRaises(DomainError) as caught:
            self.engine.outputs.save("artifact", binary["id"], {"content": "{}", "base_sha256": preview["sha256"]})
        self.assertEqual(caught.exception.status, 403)
        text = self.make("ok.md", "fine\n")
        with self.assertRaises(DomainError):
            self.engine.outputs.save("artifact", text["id"], {"content": "nul\x00byte", "base_sha256": digest(b"fine\n")})
        with self.assertRaises(DomainError):
            self.engine.outputs.save("artifact", text["id"], {"content": 5, "base_sha256": digest(b"fine\n")})
        with self.assertRaises(DomainError) as big:
            self.engine.outputs.save("artifact", text["id"], {"content": "x" * (2 * 1024 * 1024 + 1), "base_sha256": digest(b"fine\n")})
        self.assertEqual(big.exception.status, 413)
        self.assertEqual((self.engine.workspace / "ok.md").read_bytes(), b"fine\n")

    def test_files_with_secrets_hidden_in_the_preview_cannot_be_edited(self):
        secret = "sk-test-secret-value"
        item = self.make("config.md", "key = " + secret + "\n")
        hide = lambda text: str(text).replace(secret, "[redacted]")
        with patch.object(self.engine.vault, "redact", side_effect=hide):
            preview = self.engine.outputs.preview("artifact", item["id"])
            self.assertNotIn(secret, preview["content"])
            self.assertFalse(preview["editable"])
            self.assertIn("secrets", preview["readonly_reason"])
            with self.assertRaises(DomainError) as caught:
                self.engine.outputs.save("artifact", item["id"], {"content": "key = [redacted]\n", "base_sha256": preview["sha256"]})
            self.assertEqual(caught.exception.status, 403)
        self.assertIn(secret, (self.engine.workspace / "config.md").read_text())


class GrantTests(FileSaveBase):
    def grant(self, access):
        folder = Path(tempfile.mkdtemp(dir=self.root))
        self.addCleanup(lambda: None)
        project = self.store.get("projects", self.run["project_id"])
        self.store.patch("projects", project["id"], {"folder_grants": [{"id": "g1", "path": str(folder), "target_type": "folder", "access": access}]})
        target = folder / "docs" / "plan.md"
        target.parent.mkdir()
        target.write_bytes(b"plan\n")
        item = self.engine.outputs._register(target, folder, "project", {}, self.actor, self.run, {"grant_id": "g1", "source_project": project["id"]})
        return folder, target, item

    def test_read_only_grant_is_403_and_readwrite_grant_saves(self):
        folder, target, item = self.grant("read")
        preview = self.engine.outputs.preview("artifact", item["id"])
        self.assertEqual((preview["editable"], preview["readonly_reason"]), (False, "Folder is read-only for agents and edits."))
        with self.assertRaises(DomainError) as caught:
            self.engine.outputs.save("artifact", item["id"], {"content": "x\n", "base_sha256": preview["sha256"]})
        self.assertEqual(caught.exception.status, 403)
        self.assertIn("read-only", str(caught.exception))
        self.assertEqual(target.read_bytes(), b"plan\n")
        project = self.store.get("projects", self.run["project_id"])
        self.store.patch("projects", project["id"], {"folder_grants": [{**project["folder_grants"][0], "access": "readwrite"}]})
        saved = self.engine.outputs.save("artifact", item["id"], {"content": "plan v2\n", "base_sha256": preview["sha256"]})
        self.assertEqual(target.read_bytes(), b"plan v2\n")
        self.assertTrue(saved["editable"])
        self.assertEqual(self.changes()[0]["path"], "docs/plan.md")

    def test_files_outside_the_allowed_roots_are_refused(self):
        project = self.run["project_id"]
        escaping = self.store.create("artifacts", {"path": "../escape.md", "title": "x", "source": "workspace", "project_id": project, "type": "md"})
        with self.assertRaises(DomainError) as caught:
            self.engine.outputs.save("artifact", escaping["id"], {"content": "x", "base_sha256": "y"})
        self.assertEqual(caught.exception.status, 403)
        ungranted = self.store.create("artifacts", {"path": "a.md", "title": "x", "source": "project", "source_project": project, "project_id": project, "type": "md"})
        with self.assertRaises(DomainError) as no_grant:
            self.engine.outputs.save("artifact", ungranted["id"], {"content": "x", "base_sha256": "y"})
        self.assertEqual(no_grant.exception.status, 403)


class RouteAndOpenTests(FileSaveBase):
    def request(self, method, url, body=None):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service, "session", 0))
        port = server.server_port
        server.RequestHandlerClass = make_handler(self.service, "session", port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection("127.0.0.1", port)
        try:
            headers = {"Cookie": "af_session=session", "Content-Type": "application/json", "Origin": "http://127.0.0.1:%d" % port}
            conn.request(method, url, json.dumps(body) if body is not None else None, headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            conn.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_save_route_accepts_large_text_and_reports_conflicts(self):
        item = self.make("big.md", "start\n")
        base = digest(b"start\n")
        body = "line\n" * 300000
        status, data = self.request("POST", "/api/files/artifact/%s/save" % item["id"], {"content": body, "base_sha256": base})
        self.assertEqual(status, 200)
        self.assertEqual(data["sha256"], digest(body.encode()))
        status, data = self.request("POST", "/api/files/artifact/%s/save" % item["id"], {"content": "x", "base_sha256": base})
        self.assertEqual(status, 409)
        self.assertEqual(data["current_sha256"], digest(body.encode()))
        status, data = self.request("POST", "/api/files/attachment/zzz/save", {"content": "x", "base_sha256": base})
        self.assertEqual(status, 403)
        status, data = self.request("GET", "/api/files/artifact/%s/preview" % item["id"])
        self.assertEqual((status, data["editable"], data["sha256"]), (200, True, digest(body.encode())))

    def test_open_in_app_works_for_text_but_never_for_scripts_or_web_files(self):
        notes = self.make("notes.md", "hello\n")
        with patch("agentflow.outputs.subprocess.Popen") as launch, patch("agentflow.outputs.os.name", "posix"):
            self.engine.outputs.open_native("artifact", notes["id"])
        opened = Path(launch.call_args.args[0][1])
        self.assertTrue(opened.is_relative_to(self.store.directory / "open-files"))
        self.assertEqual(opened.read_bytes(), b"hello\n")
        for name in ("run.py", "tool.js", "page.html", "go.ps1", "x.sh"):
            item = self.make(name, "x\n")
            with patch("agentflow.outputs.subprocess.Popen") as launch:
                with self.assertRaises(DomainError) as caught:
                    self.engine.outputs.open_native("artifact", item["id"])
            self.assertEqual(caught.exception.status, 403)
            launch.assert_not_called()
            self.assertFalse(self.engine.outputs.preview("artifact", item["id"])["can_open"])


if __name__ == "__main__":
    unittest.main()
