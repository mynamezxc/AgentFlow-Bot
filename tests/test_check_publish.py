"""scripts/check_publish.py: passes on this repository and catches planted secrets and private data.
Every sample is assembled at runtime, so this file never contains a real-looking token."""
import contextlib
import importlib.util
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_publish", ROOT / "scripts" / "check_publish.py")
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)

NEEDED = ("dist/seed.json", "dist/app.js", "dist/index.html", "agentflow/gemini-policy.toml", "agentflow/guides/browser-workflow.md",
          "agentflow/worker-instructions.md", "scripts/Restart-AgentFlow.ps1", "scripts/AgentFlow-Common.ps1", "scripts/restart_helper.py",
          "Start-AgentFlow.ps1", "Restart-AgentFlow.cmd", "requirements-browser.txt", "requirements-ssh.txt", ".env.example", ".gitattributes",
          "tests/test_runtime.py", "tests/layout.mjs", "server.py", "README.md", "agentflow/store.py")
PRIVATE = (".agentflow/workspace.sqlite3", ".agentflow/credentials.bin", "workspaces/company/brief.md", "artifacts/backup/old.sqlite3", "audit-reports/a.md",
           "backend/app.py", "agentflow-preview.tar.gz", ".env", ".env.local", ".openai/hosting.json", "premium-audit.json", "premium-ui.json",
           "agentflow/__pycache__/x.cpython-312.pyc", "workspace.sqlite3-wal", "server.log", "cert.pem", ".DS_Store", ".work/SPEC.md", "node_modules/x/index.js")


def samples():
    """(kind fragment, line) pairs built from pieces."""
    return [
        ("Telegram", "token = '%d:%s'" % (12345678, "Ab_" * 11 + "Z1")),
        ("OpenAI", "key = 'sk-" + "a1B2" * 8 + "'"),
        ("GitHub", "gh = '" + "ghp" + "_" + "A1" * 18 + "'"),
        ("Google", "k = '" + "AIza" + "Sy" + "0" * 33 + "'"),
        ("Slack", "t = '" + "xox" + "b-" + "1234567890-abcdefghij" + "'"),
        ("AWS", "a = '" + "AKIA" + "ABCDEFGHIJKLMNOP" + "'"),
        ("PEM", "-----BEGIN RSA PRIVATE" + " KEY-----"),
        ("JWT", "jwt = '" + "eyJ" + "a" * 12 + "." + "eyJ" + "b" * 12 + "." + "c" * 12 + "'"),
        ("Authorization", "curl -H 'Authorization: Bearer " + "q" * 24 + "'"),
        ("TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN=" + "9" * 6 + ":" + "k" * 12),
        ("GATEWAY_API_KEY", "export GATEWAY_API_KEY = 'real" + "value" * 3 + "'"),
    ]


class CheckPublishTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def put(self, relative, text="x\n"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_this_repository_is_clean_when_ignored_files_are_left_out(self):
        self.assertEqual(publish.check(ROOT, use_git=False), [])

    def test_gitignore_hides_private_files_and_keeps_everything_the_app_needs(self):
        rules = publish.parse_ignore(ROOT)
        for path in NEEDED:
            self.assertFalse(publish.ignored(path, rules), path)
        for path in PRIVATE:
            self.assertTrue(publish.ignored(path, rules), path)

    def test_needed_files_exist_so_the_list_above_stays_honest(self):
        for path in NEEDED:
            self.assertTrue((ROOT / path).is_file(), path)

    def test_every_planted_secret_is_found_and_never_printed_in_full(self):
        for kind, line in samples():
            with self.subTest(kind=kind):
                findings = publish.scan_text("sample.txt", "harmless\n" + line + "\nmore\n")
                self.assertEqual([f[1] for f in findings], [2], line)
                self.assertTrue(all(len(f[2]) < len(line) + 40 and line.strip("'") not in f[2] for f in findings))

    def test_placeholders_empty_values_and_the_marker_are_not_findings(self):
        text = "\n".join(["GATEWAY_API_KEY=", "TELEGRAM_BOT_TOKEN=<your token>", "FACEBOOK_APP_SECRET=changeme", "META_VERIFY_TOKEN=...",
                          "SEO_WEBHOOK_SECRET=${SEO_SECRET}", "TELEGRAM_BOT_TOKEN=fake-for-tests",
                          "token = '%d:%s'  # publish-ok" % (12345678, "A" * 35), "const key = 'sk-short'", "Authorization: Bearer <token>",
                          "headers = {'Authorization': 'Bearer ' + token}"])
        self.assertEqual(publish.scan_text("a.md", text), [])

    def test_private_data_files_are_refused_even_when_empty(self):
        for path in (".agentflow/workspace.sqlite3", "x/.agentflow/a.json", "workspaces/company/a.md", "artifacts/b/c.txt", "db.sqlite3", "db.sqlite3-wal",
                     "credentials.bin", ".env", ".env.local", ".openai/hosting.json", "premium-audit.json", "keys/server.pem", "id.key"):
            self.put(path, "")
            self.assertTrue(publish.forbidden(path), path)
        for path in (".env.example", "agentflow/store.py", "dist/seed.json", "tests/premium-notes.json", "docs/workspaces.md"):
            self.assertIsNone(publish.forbidden(path), path)
        found = {f[0] for f in publish.check(self.root, use_git=False)}
        self.assertIn(".agentflow/workspace.sqlite3", found)
        self.assertNotIn(".env.example", found)

    def test_working_tree_walk_honours_gitignore(self):
        self.put(".gitignore", "/workspaces/\n.env\n*.log\n!keep.log\n")
        self.put("workspaces/company/a.md", "t = '%d:%s'" % (12345678, "A" * 35))
        self.put("app.log", "t = '%d:%s'" % (12345678, "A" * 35))
        self.put("keep.log", "nothing here")
        self.put("src/ok.py", "x = 1")
        self.assertEqual(publish.listing(self.root, use_git=False), [".gitignore", "keep.log", "src/ok.py"])
        self.assertEqual(publish.check(self.root, use_git=False), [])
        self.put("src/leak.py", samples()[0][1])
        self.assertEqual([(f[0], f[1]) for f in publish.check(self.root, use_git=False)], [("src/leak.py", 1)])

    def test_main_exit_codes_and_report(self):
        self.put("README.md", "hello\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(publish.main(["--root", str(self.root)]), 0)
        self.assertIn("OK", out.getvalue())
        self.put("config.env", "TELEGRAM_BOT_TOKEN=" + "7" * 10 + ":" + "Q" * 35)
        self.put(".env", "")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(publish.main(["--root", str(self.root)]), 1)
        report = out.getvalue()
        self.assertIn("config.env:1", report)
        self.assertIn(".env:", report)
        self.assertIn("git rm -r --cached", report)
        self.assertNotIn("Q" * 35, report)

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_git_mode_checks_tracked_and_addable_files_only(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.put(".gitignore", ".agentflow/\n")
        self.put(".agentflow/vault.txt", samples()[0][1])
        self.put("src/a.py", "x = 1")
        self.assertEqual(publish.check(self.root), [])
        self.put("workspaces/x.md", "private")   # not ignored here, so it would be pushed
        self.assertEqual([f[0] for f in publish.check(self.root)], ["workspaces/x.md"])

    def test_real_names_were_scrubbed_from_published_files(self):
        banned = ["2p" + "os.vn", "Cung" + "ban", "Marketing" + "-SEO", "Minh" + " Anh", "Du" + "-an"]
        for relative in publish.listing(ROOT, use_git=False):
            if relative.startswith("tests/__pycache__") or not (ROOT / relative).suffix in (".py", ".js", ".mjs", ".md", ".json", ".html", ".css", ".ps1", ".cmd", ".txt", ".toml"):
                continue
            text = (ROOT / relative).read_text(encoding="utf-8", errors="replace").lower()
            for name in banned:
                self.assertNotIn(name.lower(), text, relative)


if __name__ == "__main__":
    unittest.main()
