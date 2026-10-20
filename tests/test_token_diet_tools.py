import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_runtime as runtime
from agentflow import research
from agentflow.browser import (SNAPSHOT_CAP, SNAPSHOT_DIFF_CAP, SNAPSHOT_FULL_CAP, aria_diff, aria_section, compact_aria)
from agentflow.contract import SYSTEM_PROMPT, TOOL_SCHEMAS
from agentflow.store import DomainError

PAGE = """- banner:
  - link "Home" [ref=e1] [cursor=pointer]:
    - /url: https://shop.test/
    - text: Home
  - navigation "Main":
    - list:
      - listitem:
        - link "Catalog" [ref=e2]:
          - /url: /catalog
      - listitem:
        - link "Cart" [ref=e3]:
          - /url: /cart
- main:
  - heading "Checkout" [level=1]
  - generic:
    - generic:
      - paragraph: Review your order before paying.
      - textbox "Email":
        - /placeholder: you@example.com
      - checkbox "Gift wrap"
      - button "Pay now" [ref=e9]
- contentinfo:
  - text: Copyright 2026
"""


def big_page(rows):
    lines = ["- main:"]
    for i in range(rows):
        lines += ["  - generic:", "    - listitem:", '      - link "Product number %d with a long descriptive name":' % i,
                  "        - /url: /p/%d" % i, "      - text: Price %d USD and some descriptive copy" % i]
    lines += ["- contentinfo:", "  - text: end"]
    return "\n".join(lines) + "\n"


class FakeLocator:
    def __init__(self, page, key):
        self.page, self.key = page, key

    def aria_snapshot(self):
        return self.page.aria

    def count(self):
        return 0 if self.key in self.page.missing else 1

    def get_attribute(self, name):
        return None

    def _do(self, what, *a):
        self.page.log.append((what, self.key) + a)
        if self.page.on_action:
            self.page.on_action(self.page, what, self.key)

    def click(self):
        self._do("click")

    def fill(self, value):
        self._do("fill", value)

    def select_option(self, value):
        self._do("select", value)

    def set_checked(self, value):
        self._do("check", value)

    def press(self, value):
        self._do("press", value)


class FakePage:
    def __init__(self, aria=PAGE, url="https://shop.test/cart"):
        self.aria, self.url, self.log, self.missing, self.on_action = aria, url, [], set(), None
        self.mouse = Mock()

    def title(self):
        return "Shop"

    def locator(self, selector):
        return FakeLocator(self, selector)

    def get_by_role(self, role, name=None, exact=False):
        return FakeLocator(self, (role, name))

    def get_by_label(self, label, exact=False):
        return FakeLocator(self, ("label", label))

    def get_by_test_id(self, value):
        return FakeLocator(self, ("test", value))

    def screenshot(self, **kwargs):
        return b"png-bytes"

    def goto(self, url, **kwargs):
        self.url = url

    def set_viewport_size(self, size):
        pass


class ToolDietTests(unittest.TestCase):
    setUp = runtime.RuntimeTests.setUp
    tearDown = runtime.RuntimeTests.tearDown
    ready = runtime.RuntimeTests.ready

    def newrun(self):
        self.ready()
        return self.engine.chat({"message": "Check the shop", "agent_id": "a-lead", "channel": "dm-a-lead"})

    def call(self, name, args, run):
        return self.engine.tool(self.store.get("agents", "a-lead"), {"name": name, "arguments": args}, run)

    def browser_run(self, page, access="interact"):
        run = self.newrun()
        self.store.patch("projects", run["project_id"], {"browser_grants": [{"origin": "https://shop.test", "access": access}]})
        session = {"page": page, "version": 0, "observed": False, "state": {"blocked": []}}
        patches = [patch.object(self.engine.browser, "_session", return_value=session),
                   patch.object(self.engine.browser, "_allowed", return_value=True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return run, session

    def browse(self, run, name, args):
        return self.engine.browser._invoke(name, args, self.store.get("agents", "a-lead"), run)

    # ---- aria compaction, caps, sections --------------------------------------------------

    def test_compact_keeps_actionable_lines_and_drops_noise(self):
        text = "\n".join(compact_aria(PAGE.splitlines()))
        for keep in ('link "Catalog"', 'link "Cart"', 'heading "Checkout" [level=1]', 'button "Pay now"', 'textbox "Email"',
                     "/placeholder: you@example.com", 'checkbox "Gift wrap"', "main", "navigation", "Review your order", "Copyright 2026"):
            self.assertIn(keep, text)
        for drop in ("/url", "ref=", "cursor=", "generic", "listitem", "- list"):
            self.assertNotIn(drop, text)
        self.assertEqual(text.count("Home"), 1)  # the child text repeating the link name is folded
        self.assertLess(len(text), len(PAGE) * 0.7)

    def test_snapshot_default_is_compact_and_capped_with_continuation_note(self):
        page = FakePage(big_page(400))
        run, session = self.browser_run(page)
        result = self.browse(run, "browser.snapshot", {})
        self.assertLessEqual(len(result["snapshot"]), SNAPSHOT_CAP + 300)
        self.assertRegex(result["snapshot"], r"…\[\d+ more lines; call browser.snapshot \{full:true, section:'<landmark name>'\}")
        self.assertIn("main", result["sections"])
        self.assertEqual(result["snapshot_id"], 1)
        self.assertNotIn("/url", result["snapshot"])

    def test_full_snapshot_is_raw_and_capped_at_24k_and_section_returns_subtree(self):
        page = FakePage(big_page(400))
        run, session = self.browser_run(page)
        full = self.browse(run, "browser.snapshot", {"full": True})
        self.assertIn("/url:", full["snapshot"])
        self.assertLessEqual(len(full["snapshot"]), SNAPSHOT_FULL_CAP + 300)
        self.assertIn("more lines", full["snapshot"])
        page.aria = PAGE
        part = self.browse(run, "browser.snapshot", {"section": "navigation"})
        self.assertIn('link "Catalog"', part["snapshot"])
        self.assertNotIn("Checkout", part["snapshot"])
        with self.assertRaises(DomainError) as ctx:
            self.browse(run, "browser.snapshot", {"section": "no-such-area"})
        self.assertIn("Landmarks", str(ctx.exception))
        self.assertEqual(aria_section(PAGE.splitlines(), "contentinfo")[-1].strip(), "- text: Copyright 2026")

    # ---- diff mode ------------------------------------------------------------------------

    def test_same_url_returns_unchanged_or_only_changed_lines_new_url_returns_compact(self):
        page = FakePage()
        run, session = self.browser_run(page)
        first = self.browse(run, "browser.snapshot", {"diff": False})
        self.assertIn("snapshot", first)
        same = self.browse(run, "browser.snapshot", {})
        self.assertTrue(same["unchanged"])
        self.assertNotIn("snapshot", same)
        self.assertEqual(same["snapshot_id"], 2)
        page.aria = PAGE.replace('button "Pay now" [ref=e9]', 'button "Pay now" [disabled]\n      - alert: Email is required')
        changed = self.browse(run, "browser.snapshot", {})
        self.assertIn('+  - alert: Email is required', changed["diff"])
        self.assertIn('-  - button "Pay now"', changed["diff"])
        self.assertEqual(changed["removed"], 1)
        self.assertLessEqual(len(changed["diff"]), SNAPSHOT_DIFF_CAP)
        self.assertNotIn("snapshot", changed)
        page.url = "https://shop.test/thanks"
        moved = self.browse(run, "browser.snapshot", {})
        self.assertIn("snapshot", moved)
        self.assertNotIn("diff", moved)
        forced = self.browse(run, "browser.snapshot", {"full": True})
        self.assertIn("snapshot", forced)

    def test_oversized_diff_falls_back_to_compact_snapshot(self):
        old, new = compact_aria(big_page(300).splitlines()), compact_aria(big_page(300).replace("Price", "Cost").splitlines())
        text, added, removed = aria_diff(old, new)
        self.assertGreater(len(text), SNAPSHOT_DIFF_CAP)
        page = FakePage(big_page(300))
        run, session = self.browser_run(page)
        self.browse(run, "browser.snapshot", {})
        page.aria = big_page(300).replace("Price", "Cost")
        again = self.browse(run, "browser.snapshot", {})
        self.assertIn("snapshot", again)
        self.assertNotIn("diff", again)

    # ---- browser.act batches --------------------------------------------------------------

    def test_batch_runs_in_order_with_one_final_diff_snapshot(self):
        page = FakePage()
        run, session = self.browser_run(page)
        base = self.browse(run, "browser.snapshot", {})
        page.on_action = lambda p, what, key: setattr(p, "aria", PAGE.replace('button "Pay now" [ref=e9]', 'button "Pay now"\n      - status: Saved')) if what == "click" else None
        result = self.browse(run, "browser.act", {"snapshot_id": base["snapshot_id"], "actions": [
            {"action": "fill", "target": {"label": "Email"}, "value": "a@b.co"},
            {"action": "check", "target": {"role": "checkbox", "name": "Gift wrap"}, "checked": True},
            {"action": "click", "target": {"role": "button", "name": "Pay now"}},
            {"action": "scroll", "amount": 300}]})
        self.assertEqual([entry[0] for entry in page.log], ["fill", "check", "click"])
        self.assertEqual(result["actions_done"], 4)
        self.assertNotIn("failed", result)
        self.assertIn("status: Saved", result["diff"])
        self.assertEqual(result["snapshot_id"], base["snapshot_id"] + 1)  # exactly one snapshot for the whole batch
        page.mouse.wheel.assert_called_once_with(0, 300)

    def test_batch_stops_at_first_failure_and_reports_index(self):
        page = FakePage()
        run, session = self.browser_run(page)
        base = self.browse(run, "browser.snapshot", {})
        page.missing.add(("button", "Nope"))
        result = self.browse(run, "browser.act", {"snapshot_id": base["snapshot_id"], "actions": [
            {"action": "fill", "target": {"label": "Email"}, "value": "x"},
            {"action": "click", "target": {"role": "button", "name": "Nope"}},
            {"action": "click", "target": {"role": "button", "name": "Pay now"}}]})
        self.assertEqual(result["actions_done"], 1)
        self.assertEqual(result["failed"]["index"], 1)
        self.assertIn("missing or ambiguous", result["failed"]["error"])
        self.assertEqual(len(page.log), 1)
        self.assertTrue(result["unchanged"] or "diff" in result)

    def test_batch_keeps_security_checks(self):
        page = FakePage()
        run, session = self.browser_run(page)
        base = self.browse(run, "browser.snapshot", {})
        with self.assertRaises(DomainError):  # stale snapshot id is rejected once, before anything runs
            self.browse(run, "browser.act", {"snapshot_id": 99, "actions": [{"action": "click", "target": {"role": "button", "name": "Pay now"}}]})
        self.assertEqual(page.log, [])
        for bad in ([], [{"action": "scroll"}] * 11, ["click"], "click"):
            session["observed"] = True
            with self.assertRaises(DomainError):
                self.browse(run, "browser.act", {"snapshot_id": session["version"], "actions": bad})
        # a password control stops the batch
        page.locator = lambda selector: FakeLocator(page, selector)
        with patch.object(FakeLocator, "get_attribute", lambda self, name: "password" if name == "type" else None):
            session["observed"] = True
            result = self.browse(run, "browser.act", {"snapshot_id": session["version"], "actions": [{"action": "fill", "target": {"label": "Email"}, "value": "x"}]})
        self.assertEqual(result["failed"]["index"], 0)
        self.assertIn("owner-managed", result["failed"]["error"])
        self.assertEqual(page.log, [])
        # navigating off the granted origin mid-batch stops the remaining steps
        session["observed"] = True
        page.on_action = lambda p, what, key: setattr(p, "url", "https://elsewhere.test/")
        with patch.object(self.engine.browser, "_allowed", side_effect=lambda url, project, access="read": "shop.test" in url):
            result = self.browse(run, "browser.act", {"snapshot_id": session["version"], "actions": [
                {"action": "click", "target": {"role": "button", "name": "Pay now"}},
                {"action": "click", "target": {"role": "link", "name": "Cart"}}]})
        self.assertEqual(result["failed"]["index"], 1)
        self.assertEqual(len(page.log), 1)

    def test_single_action_still_raises_and_read_grant_cannot_act(self):
        page = FakePage()
        run, session = self.browser_run(page, access="read")
        base = self.browse(run, "browser.snapshot", {})
        with self.assertRaises(DomainError):
            self.browse(run, "browser.act", {"snapshot_id": base["snapshot_id"], "action": "click", "target": {"role": "button", "name": "Pay now"}})
        self.store.patch("projects", run["project_id"], {"browser_grants": [{"origin": "https://shop.test", "access": "interact"}]})
        page.missing.add(("button", "Gone"))
        with self.assertRaises(DomainError):
            self.browse(run, "browser.act", {"snapshot_id": base["snapshot_id"], "action": "click", "target": {"role": "button", "name": "Gone"}})
        ok = self.browse(run, "browser.act", {"snapshot_id": base["snapshot_id"], "action": "click", "target": {"role": "button", "name": "Pay now"}})
        self.assertTrue(ok["unchanged"])

    def test_open_can_capture_a_screenshot_in_the_same_call(self):
        page = FakePage(url="https://shop.test/")
        run, session = self.browser_run(page)
        with patch.object(self.engine.browser, "_open_checked", return_value=["93.184.216.34"]):
            result = self.browse(run, "browser.open", {"url": "https://shop.test/", "screenshot": True, "title": "Landing"})
        self.assertIn("snapshot", result)
        shot = result["screenshot"]
        self.assertTrue(shot["image_available"])
        self.assertEqual(self.store.get("artifacts", shot["artifact_id"])["kind"], "screenshot")
        self.assertEqual(self.store.get("artifacts", shot["artifact_id"])["title"], "Landing")
        self.assertEqual(Path(self.engine.browser.images(run)[0]["path"]).read_bytes(), b"png-bytes")
        with patch.object(self.engine.browser, "_open_checked", return_value=["93.184.216.34"]):
            plain = self.browse(run, "browser.open", {"url": "https://shop.test/"})
        self.assertNotIn("screenshot", plain)
        self.assertIn("snapshot", plain)  # open never answers with a diff

    # ---- files ----------------------------------------------------------------------------

    def write(self, name, text):
        folder = self.engine.workspace
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text, encoding="utf-8")

    def test_read_default_limit_clamp_next_offset_and_total(self):
        run = self.newrun()
        self.write("notes/long.txt", "x" * 30000)
        first = self.call("workspace.read", {"path": "notes/long.txt"}, run)
        self.assertEqual(len(first["content"]), 12000)
        self.assertEqual(first["total_chars"], 30000)
        self.assertEqual(first["next_offset"], 12000)
        clamped = self.call("workspace.read", {"path": "notes/long.txt", "limit": 90000, "offset": 100}, run)
        self.assertEqual(len(clamped["content"]), 20000)
        self.assertEqual(clamped["next_offset"], 20100)
        self.assertNotIn("outline", clamped)
        tail = self.call("workspace.read", {"path": "notes/long.txt", "offset": 20100}, run)
        self.assertIsNone(tail["next_offset"])
        self.write("notes/short.txt", "hello")
        self.assertEqual(self.call("workspace.read", {"path": "notes/short.txt"}, run), "hello")  # small files stay plain text

    def test_large_file_without_offset_gets_a_jump_outline(self):
        run = self.newrun()
        body = ["# Title"] + ["filler line %d" % i for i in range(1500)] + ["## Late section", "def target_function():", "    pass"] + ["more %d" % i for i in range(500)]
        text = "\n".join(body)
        self.write("big.md", text)
        result = self.call("workspace.read", {"path": "big.md"}, run)
        self.assertGreater(result["total_chars"], 20000)
        self.assertTrue(any("## Late section" in e for e in result["outline"]))
        self.assertFalse(any("# Title" in e for e in result["outline"]))  # already in the first chunk
        entry = next(e for e in result["outline"] if "target_function" in e)
        offset = int(entry.split()[0][1:])
        jumped = self.call("workspace.read", {"path": "big.md", "offset": offset, "limit": 200}, run)
        self.assertTrue(jumped["content"].startswith("def target_function"))
        self.assertNotIn("outline", jumped)
        self.assertLess(len(json.dumps(result["outline"])), 2200)

    def test_fingerprint(self):
        run = self.newrun()
        self.write("fp.md", "abc")
        files = self.engine.files
        first = files.fingerprint({"path": "fp.md"})
        self.assertEqual(first[0], str((self.engine.workspace / "fp.md").resolve()))
        self.assertEqual(first[1], 3)
        self.assertEqual(files.fingerprint({"path": "fp.md"}), first)
        (self.engine.workspace / "fp.md").write_text("abcd", encoding="utf-8")
        self.assertNotEqual(files.fingerprint({"path": "fp.md"}), first)
        for bad in ({"path": ""}, {"path": "../x.md"}, {"path": "missing.md"}, {"path": "notes"}):
            self.assertIsNone(files.fingerprint(bad), bad)
        self.assertIsNone(files.fingerprint({"path": "fp.md"}, project=True))  # no project_id

    def test_search_caps_matches_context_and_total(self):
        run = self.newrun()
        for n in range(6):
            self.write("s/f%d.md" % n, "\n".join("needle %d %s" % (i, "y" * 150) for i in range(20)))
        result = self.call("workspace.search", {"query": "needle"}, run)
        matches = [m for m in result if "path" in m]
        self.assertLessEqual(len(matches), 40)
        self.assertTrue(result[-1].get("note"))
        self.assertLessEqual(len(json.dumps(matches)), 6600)
        self.write("wide.md", "a" * 500 + " TARGET " + "b" * 500)
        wide = [m for m in self.call("workspace.search", {"query": "target", "path": "wide.md"}, run) if "path" in m][0]
        self.assertLessEqual(len(wide["text"]), 201)
        self.assertIn("TARGET", wide["text"])

    # ---- web.fetch ------------------------------------------------------------------------

    def fake_connection(self, html, content_type="text/html"):
        class Response:
            status = 200

            def getheader(self, name, default=""):
                return content_type if name == "Content-Type" else default

            def read(self, n):
                return html.encode()[:n]

        class Conn:
            def __init__(self, host, address, port=443):
                pass

            def request(self, *a, **k):
                pass

            def getresponse(self):
                return Response()

            def close(self):
                pass
        return Conn

    def test_web_fetch_strips_chrome_and_pages_by_offset(self):
        paragraphs = "".join("<p>Paragraph %03d %s</p>" % (i, "w" * 60) for i in range(400))
        html = ("<html><head><title>T</title><style>.a{}</style></head><body><nav><a href='/'>MENU-ITEM</a></nav>"
                "<header><h1>Real title</h1></header><main>%s</main><footer>FOOTER-LEGAL</footer><script>var x=1</script></body></html>" % paragraphs)
        resolve = lambda host, port: ["93.184.216.34"]
        with patch.object(research, "PinnedConnection", self.fake_connection(html)):
            first = research.fetch_page({"url": "https://docs.example.com/a"}, resolve=resolve)
            second = research.fetch_page({"url": "https://docs.example.com/a", "offset": first["next_offset"], "limit": 500}, resolve=resolve)
        self.assertEqual(len(first["text"]), 10000)
        self.assertGreater(first["total_chars"], 10000)
        self.assertEqual(first["next_offset"], 10000)
        for junk in ("MENU-ITEM", "FOOTER-LEGAL", "var x"):
            self.assertNotIn(junk, first["text"])
        self.assertIn("# Real title", first["text"])
        self.assertEqual(len(second["text"]), 500)
        with patch.object(research, "PinnedConnection", self.fake_connection(html)):
            big = research.fetch_page({"url": "https://docs.example.com/a", "limit": 999999}, resolve=resolve)
        self.assertEqual(len(big["text"]), 20000)

    def test_web_fetch_keeps_page_when_chrome_stripping_would_swallow_it(self):
        html = "<body><nav>" + "<p>Only content %d %s</p>" % (1, "z" * 6000) + "</nav></body>"
        with patch.object(research, "PinnedConnection", self.fake_connection(html)):
            page = research.fetch_page({"url": "https://docs.example.com/a"}, resolve=lambda host, port: ["93.184.216.34"])
        self.assertIn("Only content", page["text"])

    def test_public_page_callers_keep_their_larger_limits(self):
        html = "<p>" + "q" * 50000 + "</p>"
        with patch.object(research, "PinnedConnection", self.fake_connection(html)):
            page = research.public_page("https://docs.example.com/a", max_chars=100001, resolve=lambda host, port: ["93.184.216.34"])
        self.assertEqual(len(page["text"]), 50000)
        self.assertIsNone(page["next_offset"])

    # ---- outputs.inspect, agents.inspect, tasks.inspect ----------------------------------

    def test_outputs_inspect_is_capped_with_stats_and_paging(self):
        run = self.newrun()
        lines = "\n".join("line %d %s" % (i, "c" * 40) for i in range(2000))
        saved = self.call("workspace.write", {"path": "diff/big.md", "content": lines}, run)
        view = self.call("outputs.inspect", {"change_id": saved["change_id"]}, run)
        self.assertLessEqual(len(view["diff"]), 12000)
        self.assertEqual(view["added"], 2000)
        self.assertEqual(view["kind"], "created")
        self.assertEqual(view["next_offset"], 12000)
        self.assertIn("truncated", view["note"])
        self.assertNotIn("before", view)
        self.assertNotIn("after", view)
        later = self.call("outputs.inspect", {"change_id": saved["change_id"], "offset": 12000}, run)
        self.assertTrue(later["diff"])
        edited = self.call("workspace.write", {"path": "diff/big.md", "content": lines.replace("line 5 ", "LINE 5 ")}, run)
        small = self.call("outputs.inspect", {"change_id": edited["change_id"]}, run)
        self.assertNotIn("next_offset", small)
        self.assertIn("+LINE 5 ", small["diff"])
        self.assertLess(len(json.dumps(small)), 2000)
        other = self.engine.chat({"message": "Other project", "agent_id": "a-lead", "channel": "dm-a-lead"})
        if other["project_id"] != run["project_id"]:
            with self.assertRaises(DomainError):
                self.call("outputs.inspect", {"change_id": saved["change_id"]}, other)

    def test_agents_inspect_roster_is_compact_and_detail_caps_instructions(self):
        run = self.newrun()
        self.store.patch("agents", "a-content", {"instructions": "I" * 9000})
        roster = self.call("agents.inspect", {}, run)
        self.assertTrue(roster)
        for row in roster:
            self.assertEqual(set(row), {"id", "name", "role", "category", "parent_id", "status", "provider", "model", "skills"})
            self.assertIsInstance(row["skills"], int)
        self.assertLess(len(json.dumps(roster)), 600 * len(roster))
        detail = self.call("agents.inspect", {"agent_id": "a-content"}, run)[0]
        self.assertLessEqual(len(detail["instructions"]), 3100)
        self.assertIn("truncated", detail["instructions"])
        self.assertIn("tools", detail)

    def test_tasks_inspect_compact_list_and_capped_detail(self):
        run = self.newrun()
        made = self.call("tasks.delegate", {"title": "Audit the funnel", "description": "Check", "agent_id": "a-content"}, run)
        task_id = made["task_id"]
        self.store.patch("tasks", task_id, {"result": "R" * 20000})
        listing = self.call("tasks.inspect", {}, run)
        row = next(t for t in listing if t["id"] == task_id)
        self.assertEqual(set(row), {"id", "title", "status", "agent_id", "reviewer_id", "result"})
        self.assertLessEqual(len(row["result"]), 301)
        self.assertNotIn("description", row)
        full = self.call("tasks.inspect", {"task_id": task_id}, run)
        self.assertLessEqual(len(full["result"]), 8100)
        self.assertIn("truncated", full["result"])
        self.assertIn("description", full)

    # ---- contract -------------------------------------------------------------------------

    def test_prompt_rules_and_schemas_describe_the_new_parameters(self):
        for phrase in ("batch up to 8", "Search before reading", "never re-read unchanged files", "actions:[", "screenshot:true", "only for a new result"):
            self.assertIn(phrase, SYSTEM_PROMPT.replace("team.message is private and ", ""))
        self.assertIn("actions", TOOL_SCHEMAS["browser.act"])
        self.assertIn("screenshot", TOOL_SCHEMAS["browser.open"])
        self.assertTrue({"full", "section", "diff"} <= set(TOOL_SCHEMAS["browser.snapshot"]))
        self.assertIn("offset", TOOL_SCHEMAS["web.fetch"])
        self.assertIn("12000", TOOL_SCHEMAS["project.read"]["limit"])
        guide = (Path(__file__).resolve().parents[1] / "agentflow" / "guides" / "browser-workflow.md").read_text(encoding="utf-8")
        self.assertIn("actions:[", guide)
        self.assertLess(len(guide), 2500)


if __name__ == "__main__":
    unittest.main()
