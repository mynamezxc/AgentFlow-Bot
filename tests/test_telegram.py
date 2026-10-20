"""Telegram bridge tests. A fake Telegram API replaces the HTTP helper; the token below is an obvious fake and the
vault is never written (the bridge's token reader is patched)."""
import base64
import http.client
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import test_runtime as runtime
from agentflow import telegram as tg
from agentflow.store import DomainError
from agentflow.telegram import TelegramError
from server import make_handler

FAKE_TOKEN = "123456:FAKE_TEST_TOKEN_not_a_real_secret_000"
CHAT = -1001234567890
OWNER = 7
JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" * 4 + b"\xff\xd9"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


class FakeTelegram:
    """Records every call; `script[method]` is a list of exceptions raised by the next calls of that method."""

    def __init__(self):
        self.calls, self.updates, self.script, self.next_id = [], [], {}, 100
        self.tokens = set()

    def call(self, token, method, params=None, timeout=60, file=None):
        self.tokens.add(token)
        self.calls.append((method, dict(params or {}), file))
        queue = self.script.get(method)
        if queue:
            raise queue.pop(0)
        if method == "getMe":
            return {"id": 1, "username": "af_test_bot"}
        if method == "getUpdates":
            out, self.updates = self.updates, []
            if not out:
                time.sleep(0.01)   # a real long poll would wait; keep background pollers from spinning
            return out
        if method == "getFile":
            return {"file_path": "files/" + params["file_id"]}
        if method in ("sendMessage", "sendPhoto", "sendDocument"):
            self.next_id += 1
            return {"message_id": self.next_id}
        return True

    def sent(self, method="sendMessage"):
        return [p for m, p, _ in self.calls if m == method]

    def texts(self):
        return [tg.plain(p["text"]) for p in self.sent()]

    def clear(self):
        self.calls.clear()


class Stopper:
    """Stands in for the bridge's stop event: records the waits and ends the loop after `limit` of them."""

    def __init__(self, limit):
        self.waits, self.limit = [], limit

    def is_set(self):
        return False

    def wait(self, seconds=None):
        self.waits.append(seconds)
        return len(self.waits) >= self.limit

    def set(self):
        pass

    def clear(self):
        pass


class TelegramBase(unittest.TestCase):
    setUp0 = runtime.RuntimeTests.setUp
    tearDown = runtime.RuntimeTests.tearDown
    ready = runtime.RuntimeTests.ready

    def setUp(self):
        self.setUp0()
        self.bridge = self.engine.telegram
        self.fake = FakeTelegram()
        self.bridge.http = self.fake.call
        self.bridge.download = lambda token, path, limit: PNG
        self.bridge.token = lambda: FAKE_TOKEN
        self.bridge.attach()
        self.bridge.remember(username="af_test_bot")
        self.update_id = 1000

    # ---- helpers -----------------------------------------------------------------------------------------------------
    def link(self, owners=(OWNER,), **extra):
        self.store.settings({"telegram": {"chat_id": str(CHAT), "chat_title": "Team room", "chat_type": "supergroup",
            "owner_user_ids": list(owners), "linked_at": "2026-01-01T00:00:00Z", "default_agent_id": "a-lead",
            "forward": {"questions": True, "permissions": True, "agent_messages": True, "status_digest": False, "files": True}, **extra},
            "telegram_chats": str(CHAT)})

    def message(self, text="", user=OWNER, chat=CHAT, **extra):
        self.update_id += 1
        body = {"message_id": self.update_id, "chat": {"id": chat, "type": "supergroup", "title": "Team room"},
                "from": {"id": user, "username": "boss" if user == OWNER else "guest%d" % user}, "date": 1, **extra}
        if text:
            body["text"] = text
        return {"update_id": self.update_id, "message": body}

    def send(self, text="", **kwargs):
        update = self.message(text, **kwargs)
        result = self.bridge.handle_update(update)
        self.bridge.run_jobs(force=True)
        self.bridge.flush()
        return result

    def say_out(self, text, **kwargs):
        """Send an owner message and return the plain texts the bot replied with."""
        self.fake.clear()
        self.send(text, **kwargs)
        return self.fake.texts()

    def callback(self, data, message_id=555, user=OWNER, chat=CHAT):
        self.update_id += 1
        update = {"update_id": self.update_id, "callback_query": {"id": "cq%d" % self.update_id, "data": data, "from": {"id": user, "username": "boss"},
                  "message": {"message_id": message_id, "chat": {"id": chat}}}}
        result = self.bridge.handle_update(update)
        self.bridge.run_jobs(force=True)
        self.bridge.flush()
        return result

    def buttons(self, params):
        return [b for row in params.get("reply_markup", {}).get("inline_keyboard", []) for b in row]

    def press(self, params, label):
        button = next(b for b in self.buttons(params) if b["text"] == label)
        return self.callback(button["callback_data"])

    def ask(self, question="Pick one?", options=("Yes", "No"), **extra):
        result = self.engine.team.ask(self.store.get("agents", "a-lead"), {"question": question, "options": list(options), "blocking": False, **extra},
                                      self.bridge.anchor_run(), _routed=True)
        return result["decision_id"]

    def events(self, title):
        return [e for e in self.store.all("events") if e["title"] == title]


class PollingTests(TelegramBase):
    def test_setup_registers_commands_and_drops_webhook_in_polling_mode(self):
        self.bridge.setup()
        methods = [m for m, _, _ in self.fake.calls]
        self.assertEqual(methods[:2], ["getMe", "deleteWebhook"])
        self.assertIs(self.fake.sent("deleteWebhook")[0]["drop_pending_updates"], False)
        commands = [c["command"] for c in self.fake.sent("setMyCommands")[0]["commands"]]
        self.assertEqual(commands, ["help", "status", "showall", "get", "set", "agents", "goal", "files", "approve", "stop", "restart", "unlink"])
        self.assertEqual(self.bridge.state()["username"], "af_test_bot")

    def test_webhook_mode_keeps_the_webhook(self):
        self.store.settings({"telegram_mode": "webhook"})
        self.bridge.setup()
        self.assertEqual(self.fake.sent("deleteWebhook"), [])
        self.assertEqual(self.fake.sent("getMe") and len(self.fake.sent("setMyCommands")), 1)

    def test_offset_is_persisted_and_old_updates_are_not_handled_twice(self):
        self.link()
        first = self.message("hello")
        self.fake.updates = [first]
        with patch.object(self.engine, "chat") as chat:
            self.assertEqual(self.bridge.poll_once(), 1)
            self.assertEqual(self.bridge.state()["offset"], first["update_id"] + 1)
            self.assertEqual(json.loads(self.bridge.state_path.read_text(encoding="utf-8"))["offset"], first["update_id"] + 1)
            self.fake.updates = [first]   # Telegram redelivers (for example after a crash before the offset moved)
            self.bridge.poll_once()
            self.assertEqual(chat.call_count, 1)
        polls = self.fake.sent("getUpdates")
        self.assertEqual(polls[0]["offset"], 0)
        self.assertEqual(polls[1]["offset"], first["update_id"] + 1)
        self.assertEqual(polls[0]["allowed_updates"], ["message", "edited_message", "callback_query", "my_chat_member"])
        reloaded = tg.TelegramBridge(self.engine)
        self.assertEqual(reloaded.state()["offset"], first["update_id"] + 1)

    def test_unauthorized_token_stops_polling_with_an_event(self):
        self.fake.script["getUpdates"] = [TelegramError(401, "Unauthorized")]
        self.bridge.poll_loop()
        self.assertEqual(self.bridge.halted, "401")
        self.assertEqual(len(self.events("Telegram token rejected")), 1)
        self.assertFalse(self.bridge.public_status()["connected"])
        self.assertEqual(len(self.fake.sent("getUpdates")), 1)

    def test_conflict_stops_polling_with_a_clear_event(self):
        self.fake.script["getUpdates"] = [TelegramError(409, "Conflict: terminated by other getUpdates request")]
        self.bridge.poll_loop()
        self.assertEqual(self.bridge.halted, "409")
        event = self.events("Telegram polling stopped")[0]
        self.assertIn("Another program or a webhook", event["detail"])

    def test_rate_limit_waits_for_retry_after_and_errors_back_off(self):
        self.assertEqual(self.bridge.on_error(TelegramError(429, "Too Many Requests", retry_after=7)), 7)
        self.assertEqual(self.bridge.on_error(TelegramError(429, "Too Many Requests", retry_after=100000)), 120)
        stopper = Stopper(3)
        self.bridge.stop = stopper
        self.fake.script["getMe"] = [TelegramError(0, "Cannot reach Telegram")] * 3
        self.bridge.poll_loop()
        self.assertEqual(stopper.waits, [2, 4, 8])
        self.fake.calls.clear()
        stopper = Stopper(1)
        self.bridge.stop = stopper
        self.fake.script["getUpdates"] = [TelegramError(429, "Too Many Requests", retry_after=9)]
        self.bridge.poll_loop()
        self.assertEqual(stopper.waits, [9])

    def test_one_failing_update_does_not_stop_the_others(self):
        self.link()
        self.fake.updates = [self.message("boom"), self.message("fine")]
        with patch.object(self.engine, "chat", side_effect=[RuntimeError("bad"), {}]) as chat:
            self.assertEqual(self.bridge.poll_once(), 2)
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(self.bridge.state()["offset"], self.update_id + 1)
        self.assertEqual(len(self.events("Telegram update failed")), 1)

    def test_start_without_a_token_runs_nothing(self):
        self.bridge.token = lambda: ""
        self.assertFalse(self.bridge.start())
        self.assertEqual(self.bridge.threads, [])

    def test_start_runs_polling_and_close_stops_it(self):
        self.assertTrue(self.bridge.start())
        try:
            deadline = time.time() + 5
            while not self.fake.sent("getUpdates") and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(self.fake.sent("getUpdates"))
            self.assertTrue(self.bridge.public_status()["connected"])
        finally:
            self.bridge.stop.set()
            self.bridge.close()
        self.assertEqual(self.bridge.threads, [])


class TokenSafetyTests(TelegramBase):
    def raise_http(self, error):
        return patch.object(tg.urllib.request, "urlopen", side_effect=error)

    def test_http_errors_never_contain_the_token(self):
        url = "https://api.telegram.org/bot%s/getMe" % FAKE_TOKEN
        body = json.dumps({"ok": False, "description": "Unauthorized for " + url}).encode()
        http_error = urllib.error.HTTPError(url, 401, "Unauthorized", {}, MagicMock(read=lambda: body))
        for error in (http_error, urllib.error.URLError("failed " + url), OSError(url)):
            with self.raise_http(error), self.assertRaises(TelegramError) as caught:
                tg.http_call(FAKE_TOKEN, "getMe")
            self.assertNotIn(FAKE_TOKEN, str(caught.exception))
            self.assertNotIn(FAKE_TOKEN.split(":")[1], caught.exception.description)

    def test_redact_handles_token_shaped_text_and_events(self):
        self.assertNotIn(FAKE_TOKEN, tg.redact("GET /bot%s/getMe failed" % FAKE_TOKEN, FAKE_TOKEN))
        self.assertNotIn("FAKE_TEST_TOKEN", tg.redact("bot123456:FAKE_TEST_TOKEN_not_a_real_secret_000 x"))
        self.bridge.event("Telegram problem", "Failed at https://api.telegram.org/bot%s/getUpdates" % FAKE_TOKEN)
        self.assertNotIn(FAKE_TOKEN, json.dumps(self.store.all("events")))

    def test_multipart_upload_carries_the_file_and_a_safe_name(self):
        captured = {}

        def fake_open(request, timeout=0):
            captured["body"], captured["type"] = request.data, request.get_header("Content-type")
            return MagicMock(__enter__=lambda s: MagicMock(), __exit__=lambda *a: False)
        with patch.object(tg.urllib.request, "urlopen", fake_open), patch.object(tg.json, "load", return_value={"ok": True, "result": {"message_id": 1}}):
            tg.http_call(FAKE_TOKEN, "sendDocument", {"chat_id": "1", "caption": "c"}, file=("document", 'a"b.txt', b"DATA", "text/plain"))
        self.assertIn("multipart/form-data; boundary=", captured["type"])
        self.assertIn(b"DATA", captured["body"])
        self.assertIn(b'filename="a_b.txt"', captured["body"])

    def test_status_and_state_never_expose_the_token(self):
        self.link()
        self.bridge.setup()
        self.assertNotIn(FAKE_TOKEN, json.dumps(self.bridge.public_status()))
        self.assertNotIn(FAKE_TOKEN, json.dumps(self.store.settings()))
        self.assertNotIn(FAKE_TOKEN, self.bridge.state_path.read_text(encoding="utf-8"))


class LinkingTests(TelegramBase):
    def test_unknown_chat_creates_one_owner_decision_and_one_notice(self):
        with patch.object(self.engine, "chat") as chat:
            self.assertEqual(self.send("hello bot", chat=-555), "asked")
            self.assertEqual(self.send("anyone there?", chat=-555), "pending")
        chat.assert_not_called()
        decisions = [d for d in self.store.all("decisions") if d.get("action_handler") == "telegram_link"]
        self.assertEqual(len(decisions), 1)
        question = decisions[0]
        self.assertEqual(question["agent_id"], "a-lead")
        self.assertEqual(question["channel"], "dm-a-lead")
        self.assertIn("Link Telegram supergroup 'Team room' (id -555) to AgentFlow? Messages from @boss there will act as you.", question["question"])
        self.assertEqual([o["label"] for o in question["actions"]], ["Link", "Ignore"])
        self.assertEqual(self.fake.texts(), ["Waiting for approval in AgentFlow."])
        self.assertEqual(self.store.settings().get("telegram") or {}, {})

    def test_link_answer_writes_settings_and_owner(self):
        self.send("hello", chat=-555)
        decision = next(d for d in self.store.all("decisions") if d.get("action_handler") == "telegram_link")
        self.engine.team.answer(decision["id"], {"answer": "Link"})
        settings = self.store.settings()
        cfg = settings["telegram"]
        self.assertEqual((cfg["chat_id"], cfg["chat_title"], cfg["chat_type"], cfg["owner_user_ids"]), ("-555", "Team room", "supergroup", [OWNER]))
        self.assertEqual(cfg["default_agent_id"], "a-lead")
        self.assertEqual(cfg["forward"], {"questions": True, "permissions": True, "agent_messages": True, "status_digest": False, "files": True})
        self.assertTrue(cfg["linked_at"])
        self.assertEqual(settings["telegram_chats"], "-555")
        self.bridge.flush()
        self.assertEqual(len(self.fake.texts()), 2)   # "Waiting for approval" + the one greeting
        self.assertTrue(self.fake.texts()[-1].startswith("Atlas - "))
        self.assertEqual(self.bridge.state().get("pending") or {}, {})
        with patch.object(self.engine, "chat") as chat:
            self.assertEqual(self.send("now it works", chat=-555), "routed:a-lead")
        chat.assert_called_once()

    def link_and_collect(self, chat=-555):
        self.send("hello", chat=chat)
        decision = next(d for d in self.store.all("decisions") if d.get("action_handler") == "telegram_link")
        self.fake.clear()
        self.engine.team.answer(decision["id"], {"answer": "Link"})
        self.bridge.run_jobs(force=True)
        self.bridge.flush()
        return self.fake.sent()

    def test_linking_posts_one_deterministic_greeting_and_a_web_note(self):
        sent = self.link_and_collect()
        self.assertEqual(len(sent), 1)
        atlas = self.store.get("agents", "a-lead")
        self.assertEqual(sent[0]["text"], "<b>%s - %s</b>: %s" % (atlas["name"], tg.esc(atlas["role"]), tg.HELLO["en"]))
        self.assertEqual(sent[0]["parse_mode"], "HTML")
        notes = [m for m in self.store.all("messages") if m["content"] == "Telegram linked: Team room"]
        self.assertEqual(len(notes), 1)
        self.assertEqual((notes[0]["agent_id"], notes[0]["channel"], notes[0]["audience"]), ("a-lead", "dm-a-lead", "owner"))
        self.assertEqual([r for r in self.store.all("runs") if r.get("kind") == "chat"], [])   # no model call
        self.bridge.run_jobs(force=True)
        self.bridge.flush()
        self.assertEqual(len(self.fake.sent()), 1)   # the web note is not echoed back

    def test_greeting_follows_the_owners_language(self):
        self.ready("a-lead")
        self.engine.chat({"message": "Chào Atlas, hôm nay làm việc gì nhé?", "agent_id": "a-lead", "channel": "dm-a-lead"})
        sent = self.link_and_collect()
        self.assertEqual(len(sent), 1)
        self.assertIn("Xin chào! Đây là tin nhắn test — Telegram đã kết nối với AgentFlow. Gõ /help để xem lệnh.", sent[0]["text"])
        self.assertTrue(sent[0]["text"].startswith("<b>Atlas - "))

    def test_greeting_uses_the_default_agents_name(self):
        self.store.settings({"telegram": {"default_agent_id": "a-seo"}})
        self.assertTrue(self.bridge.hello_html().startswith("<b>Nova - "))

    def test_ignore_blocks_the_chat_without_further_replies(self):
        self.send("hello", chat=-777)
        decision = next(d for d in self.store.all("decisions") if d.get("action_handler") == "telegram_link")
        self.engine.team.answer(decision["id"], {"answer": "Ignore"})
        self.assertFalse(self.bridge.linked())
        self.fake.clear()
        self.assertEqual(self.send("please?", chat=-777), "blocked")
        self.assertEqual(self.fake.sent(), [])
        self.assertEqual(len([d for d in self.store.all("decisions") if d.get("action_handler") == "telegram_link"]), 1)
        self.assertEqual(len(self.events("Telegram chat ignored")), 1)

    def test_linking_a_second_chat_replaces_the_first_and_keeps_legacy_list_in_sync(self):
        self.link()
        self.send("hello", chat=-888)
        decision = next(d for d in self.store.all("decisions") if d.get("action_handler") == "telegram_link")
        self.assertIn("replaces the current link", decision["question"])
        self.engine.team.answer(decision["id"], {"answer": "Link"})
        self.assertEqual(self.store.settings()["telegram_chats"], "-888")

    def test_link_decision_is_not_mirrored_to_telegram(self):
        self.link()
        self.send("hello", chat=-888)
        self.assertEqual([p for p in self.fake.sent() if p.get("reply_markup") and "Link Telegram" in p["text"]], [])

    def test_non_owner_is_ignored_with_a_single_notice(self):
        self.link()
        with patch.object(self.engine, "chat") as chat:
            self.fake.clear()
            self.assertEqual(self.send("do things", user=99), "stranger")
            self.assertEqual(self.send("do more", user=99), "ignored")
            self.assertEqual(self.send("/restart", user=99), "ignored")
            bot = self.message("hi", user=5)
            bot["message"]["from"]["is_bot"] = True
            self.assertEqual(self.bridge.handle_update(bot), "ignored")
        chat.assert_not_called()
        self.assertEqual(len(self.fake.sent()), 1)
        self.assertEqual(self.store.all("decisions"), [])

    def test_non_owner_cannot_press_buttons(self):
        self.link()
        decision_id = self.ask()
        self.bridge.run_jobs(force=True)
        self.bridge.flush()
        params = self.fake.sent()[-1]
        button = self.buttons(params)[0]
        self.fake.clear()
        self.assertEqual(self.callback(button["callback_data"], user=99), "denied")
        self.assertIs(self.fake.sent("answerCallbackQuery")[0]["show_alert"], True)
        self.assertEqual(self.store.get("decisions", decision_id)["status"], "open")

    def test_owner_ids_are_edited_only_through_validated_web_settings(self):
        with self.assertRaises(DomainError):
            self.service.settings({"telegram": {"owner_user_ids": [1]}})   # nothing linked yet
        with self.assertRaises(DomainError):
            self.service.settings({"telegram": {"chat_id": "-1"}})
        self.link()
        self.service.settings({"telegram": {"owner_user_ids": [OWNER, 8]}})
        self.assertEqual(self.store.settings()["telegram"]["owner_user_ids"], [OWNER, 8])
        self.assertEqual(self.store.settings()["telegram"]["chat_id"], str(CHAT))
        for bad in ([], [0], ["x"], [True], list(range(1, 12))):
            with self.assertRaises(DomainError):
                self.service.settings({"telegram": {"owner_user_ids": bad}})
        with self.assertRaises(DomainError):
            self.service.settings({"telegram": {"forward": {"files": "yes"}}})
        with self.assertRaises(DomainError):
            self.service.settings({"telegram": {"default_agent_id": "a-missing"}})
        with self.assertRaises(DomainError):
            self.service.settings({"telegram_mode": "carrier-pigeon"})
        self.service.settings({"telegram_mode": "webhook", "telegram": {"forward": {"files": False}, "default_agent_id": "a-seo"}})
        self.assertEqual(self.bridge.mode(), "webhook")
        self.assertEqual(self.bridge.default_agent(), "a-seo")
        self.assertFalse(self.bridge.config()["forward"]["files"])
        self.assertTrue(self.bridge.config()["forward"]["questions"])


class RoutingTests(TelegramBase):
    def setUp(self):
        super().setUp()
        self.link()

    def route(self, text, **extra):
        with patch.object(self.engine, "chat", return_value={}) as chat:
            result = self.send(text, **extra)
        return result, chat

    def test_plain_text_goes_to_the_default_agent_dm(self):
        result, chat = self.route("what is the plan?")
        self.assertEqual(result, "routed:a-lead")
        chat.assert_called_once_with({"agent_id": "a-lead", "channel": "dm-a-lead", "message": "what is the plan?", "attachment_ids": []})

    def test_default_agent_is_configurable(self):
        self.service.settings({"telegram": {"default_agent_id": "a-seo"}})
        result, chat = self.route("hello")
        self.assertEqual(result, "routed:a-seo")
        self.assertEqual(chat.call_args[0][0]["channel"], "dm-a-seo")

    def test_at_name_and_colon_name_route_to_that_member(self):
        for text, agent, body in (("@Nova write the meta description", "a-seo", "write the meta description"),
                                  ("luna: draft a post", "a-content", "draft a post"),
                                  ("@af_test_bot @Pixel fix the bug", "a-code", "fix the bug"),
                                  ("@nova, please check", "a-seo", "please check")):
            with self.subTest(text=text):
                result, chat = self.route(text)
                self.assertEqual(result, "routed:" + agent)
                self.assertEqual(chat.call_args[0][0]["message"], body)
                self.assertEqual(chat.call_args[0][0]["channel"], "dm-" + agent)

    def test_unrelated_text_that_mentions_a_name_stays_with_the_default_agent(self):
        result, chat = self.route("Novara is a city")
        self.assertEqual(result, "routed:a-lead")
        self.assertEqual(chat.call_args[0][0]["message"], "Novara is a city")

    def test_name_without_a_message_is_an_error_reply(self):
        self.fake.clear()
        with patch.object(self.engine, "chat") as chat:
            self.assertEqual(self.send("@Nova"), "error")
        chat.assert_not_called()
        self.assertEqual(self.fake.texts(), ["Add a message after the name."])

    def test_reply_to_an_agent_message_routes_to_that_agent(self):
        self.bridge.mutate(lambda s: s.setdefault("msgs", {}).__setitem__("501", {"t": "agent", "id": "a-code"}))
        result, chat = self.route("and the tests?", reply_to_message={"message_id": 501})
        self.assertEqual(result, "routed:a-code")
        self.assertEqual(chat.call_args[0][0]["channel"], "dm-a-code")

    def test_reply_to_a_decision_message_answers_it(self):
        decision_id = self.ask()
        self.bridge.run_jobs(force=True)
        self.bridge.flush()
        message_id = self.bridge.state()["posted"][decision_id]["message_id"]
        with patch.object(self.engine, "chat") as chat:
            self.assertEqual(self.send("2", reply_to_message={"message_id": message_id}), "answered")
        chat.assert_not_called()
        self.assertEqual(self.store.get("decisions", decision_id)["answer"], "No")

    def test_reply_with_text_that_is_not_an_option_is_rejected_and_keeps_the_question_open(self):
        decision_id = self.ask()
        self.bridge.run_jobs(force=True)
        self.bridge.flush()
        message_id = self.bridge.state()["posted"][decision_id]["message_id"]
        self.fake.clear()
        self.assertEqual(self.send("maybe", reply_to_message={"message_id": message_id}), "error")
        self.assertIn("Choose one of: 1. Yes; 2. No", self.fake.texts()[0])
        self.assertEqual(self.store.get("decisions", decision_id)["status"], "open")

    def test_photo_becomes_an_attachment(self):
        self.bridge.download = lambda token, path, limit: JPEG
        photo = [{"file_id": "small", "file_unique_id": "u1", "width": 10, "height": 10, "file_size": 100},
                 {"file_id": "big", "file_unique_id": "u2", "width": 100, "height": 100, "file_size": 900}]
        result, chat = self.route("", photo=photo, caption="look at this")
        self.assertEqual(result, "routed:a-lead")
        self.assertEqual(self.fake.sent("getFile"), [{"file_id": "big"}])
        args = chat.call_args[0][0]
        self.assertEqual(args["message"], "look at this")
        self.assertEqual(len(args["attachment_ids"]), 1)

    def test_document_over_20_mb_is_refused_before_download(self):
        self.fake.clear()
        result, chat = self.route("", document={"file_id": "d", "file_name": "huge.pdf", "file_size": 21 * 1024 * 1024}, caption="read")
        self.assertEqual(result, "error")
        chat.assert_not_called()
        self.assertEqual(self.fake.sent("getFile"), [])
        self.assertIn("20 MB", self.fake.texts()[0])

    def test_edited_and_empty_updates_are_ignored(self):
        self.assertEqual(self.bridge.handle_update({"update_id": 1, "edited_message": {"chat": {"id": CHAT}}}), "ignored")
        self.assertEqual(self.bridge.handle_update({"update_id": 2, "message": {"chat": {}}}), "ignored")


class DecisionTests(TelegramBase):
    def setUp(self):
        super().setUp()
        self.link()

    def post(self):
        self.bridge.run_jobs(force=True)
        self.bridge.flush()

    def test_decision_is_posted_with_buttons_and_answer_edits_the_message(self):
        decision_id = self.ask("Ship <the> release & announce?", ("Ship", "Wait"), context="Release 1.2")
        self.post()
        params = self.fake.sent()[-1]
        self.assertIn("<b>Atlas</b>", params["text"])
        self.assertIn("Ship &lt;the&gt; release &amp; announce?", params["text"])
        self.assertEqual([b["text"] for b in self.buttons(params)], ["Ship", "Wait"])
        self.assertTrue(all(len(b["callback_data"].encode()) <= 64 for b in self.buttons(params)))
        message_id = self.bridge.state()["posted"][decision_id]["message_id"]
        self.fake.clear()
        self.assertEqual(self.callback(self.buttons(params)[0]["callback_data"], message_id=message_id), "callback")
        decision = self.store.get("decisions", decision_id)
        self.assertEqual((decision["status"], decision["answer"]), ("answered", "Ship"))
        callback = self.fake.sent("answerCallbackQuery")[0]
        self.assertEqual(callback["text"], "Answered")
        edit = self.fake.sent("editMessageText")[0]
        self.assertEqual(edit["message_id"], message_id)
        self.assertIn("✅ <b>Ship</b> — by @boss", edit["text"])
        self.assertEqual(edit["reply_markup"], {"inline_keyboard": []})

    def test_answering_in_the_web_app_edits_the_telegram_message_too(self):
        decision_id = self.ask()
        self.post()
        self.fake.clear()
        self.engine.team.answer(decision_id, {"answer": "No"})
        self.post()
        edit = self.fake.sent("editMessageText")[0]
        self.assertIn("✅ <b>No</b> — by AgentFlow web", edit["text"])
        self.assertEqual(self.fake.sent("answerCallbackQuery"), [])

    def test_answered_decision_is_edited_only_once_and_a_second_button_press_is_refused(self):
        decision_id = self.ask()
        self.post()
        params = self.fake.sent()[-1]
        first, second = self.buttons(params)
        self.callback(first["callback_data"])
        self.fake.clear()
        self.callback(second["callback_data"])
        self.assertEqual(self.fake.sent("editMessageText"), [])
        self.assertIn("already answered", self.fake.sent("answerCallbackQuery")[0]["text"])
        self.assertEqual(self.store.get("decisions", decision_id)["answer"], "Yes")

    def test_expired_button_is_reported(self):
        self.fake.clear()
        self.assertEqual(self.callback("cb:does-not-exist"), "expired")
        self.assertIn("expired", self.fake.sent("answerCallbackQuery")[0]["text"])

    def test_free_text_question_offers_reply_hint_and_accepts_any_text(self):
        decision_id = self.ask("Which name?", (), kind="input")
        self.post()
        params = self.fake.sent()[-1]
        self.assertEqual(self.buttons(params), [])
        self.assertIn("Reply to this message with your answer.", params["text"])
        message_id = self.bridge.state()["posted"][decision_id]["message_id"]
        self.assertEqual(self.send("Project Phoenix", reply_to_message={"message_id": message_id}), "answered")
        self.assertEqual(self.store.get("decisions", decision_id)["answer"], "Project Phoenix")

    def test_question_and_permission_switches_are_respected(self):
        self.service.settings({"telegram": {"forward": {"questions": False}}})
        self.ask("Quiet question?")
        self.post()
        self.assertEqual(self.fake.sent(), [])

    def test_equal_open_questions_are_posted_once(self):
        first = self.ask("Same question?")
        second = self.engine.team.ask(self.store.get("agents", "a-seo"), {"question": "Same question?", "options": ["Yes", "No"], "blocking": False},
                                      self.bridge.anchor_run(), _routed=True)["decision_id"]
        self.post()
        posted = [p for p in self.fake.sent() if "Same question?" in p["text"]]
        self.assertEqual(len(posted), 1, (first, second))

    def worker(self, actor="a-code"):
        self.ready(actor)
        return self.engine.chat({"message": "Review project source", "agent_id": actor, "channel": "dm-" + actor})

    def test_permission_requested_by_several_members_is_posted_once(self):
        first = self.worker("a-code")
        self.ready("a-seo")
        second = self.engine.enqueue("a-seo", "Read same project", "peer", project_id=first["project_id"], root_id=first["root_id"])
        folder = self.root / "project source"
        folder.mkdir()
        (folder / "README.md").write_text("source", encoding="utf-8")
        ids = []
        for run in (first, second):
            actor = self.store.get("agents", run["agent_id"])
            ids.append(self.engine.tool(actor, {"name": "files.request_access", "arguments": {"path": str(folder), "reason": "Review"}}, run)["decision_id"])
        self.assertEqual(ids[0], ids[1])
        self.post()
        self.post()
        asked = [p for p in self.fake.sent() if "needs permission" in p["text"]]
        self.assertEqual(len(asked), 1)
        self.assertIn("<b>Atlas</b>", asked[0]["text"])
        self.assertIn("Requested by: ", asked[0]["text"])
        self.assertTrue(self.buttons(asked[0]))
        self.assertEqual(len([d for d in self.store.all("decisions") if d["status"] == "open"]), 1)


class MirrorTests(TelegramBase):
    def setUp(self):
        super().setUp()
        self.link()
        self.run_record = self.engine.chat({"agent_id": "a-seo", "message": "Write copy", "channel": "dm-a-seo"})
        self.fake.clear()
        self.bridge.run_jobs(force=True)
        self.bridge.flush()
        self.fake.clear()

    def emit(self, agent, text, kind="chat", **extra):
        run = self.run_record if agent == "a-seo" else {**self.run_record, "agent_id": agent}
        message = self.engine.team.emit(agent, text, run, kind, **extra)
        self.bridge.run_jobs(force=True)
        self.bridge.flush()
        return message

    def test_owner_visible_messages_from_every_member_are_mirrored_with_name_and_role(self):
        self.emit("a-seo", "First draft is ready")
        self.emit("a-code", "Build is green")
        texts = self.fake.texts()
        seo = self.store.get("agents", "a-seo")
        self.assertEqual(texts[0], "%s · %s\nFirst draft is ready" % (seo["name"], seo["role"]))
        self.assertTrue(texts[1].startswith(self.store.get("agents", "a-code")["name"] + " ·"))
        self.assertIn("<b>%s</b> · %s" % (seo["name"], tg.esc(seo["role"])), self.fake.sent()[0]["text"])

    def test_internal_activity_and_the_owners_own_messages_are_not_mirrored(self):
        self.emit("a-seo", "Asking a peer", "peer")
        self.emit("a-seo", "Handing off", "handoff")
        self.emit("a-seo", "Quiet note", "chat", audience="internal")
        self.store.create("messages", {"agent_id": "a-seo", "role": "user", "content": "Owner wrote this", "channel": "dm-a-seo", "kind": "chat"})
        self.store.create("messages", {"agent_id": "a-seo", "role": "assistant", "content": "From telegram thread", "channel": "telegram:1", "kind": "chat", "audience": "owner"})
        self.bridge.run_jobs(force=True)
        self.bridge.flush()
        self.assertEqual(self.fake.sent(), [])

    def test_agent_event_cards_are_mirrored(self):
        self.emit("a-lead", "Created agent Quill", "agent_event")
        self.assertIn("Created agent Quill", self.fake.texts()[0])

    def test_message_text_is_html_escaped_and_markdown_is_rendered(self):
        self.emit("a-seo", "Use <script>alert(1)</script> & **bold** with `code`")
        text = self.fake.sent()[0]["text"]
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &amp; <b>bold</b> with <code>code</code>", text)
        self.assertEqual(self.fake.sent()[0]["parse_mode"], "HTML")

    def test_long_messages_are_split_below_the_4096_limit(self):
        paragraph = ("<tag> & " * 40) + "\n"
        self.emit("a-seo", paragraph * 60)
        sent = self.fake.sent()
        self.assertGreater(len(sent), 1)
        self.assertTrue(all(len(p["text"]) <= 4096 for p in sent), [len(p["text"]) for p in sent])
        self.assertTrue(sent[0]["text"].startswith("<b>"))
        self.assertFalse(sent[1]["text"].startswith("<b>"))
        joined = "".join(tg.plain(p["text"]) for p in sent)
        self.assertEqual(joined.count("<tag>"), 40 * 60)

    def test_one_unbroken_line_is_cut_and_still_fits(self):
        self.bridge.say(CHAT, "<" * 9000)
        self.bridge.flush()
        sent = self.fake.sent()
        self.assertGreaterEqual(len(sent), 3)
        self.assertTrue(all(len(p["text"]) <= 4096 for p in sent))
        self.assertEqual(sum(tg.plain(p["text"]).count("<") for p in sent), 9000)

    def test_agent_message_switch_turns_text_mirroring_off(self):
        self.service.settings({"telegram": {"forward": {"agent_messages": False}}})
        self.emit("a-seo", "Hidden from Telegram")
        self.assertEqual(self.fake.sent(), [])

    def test_a_message_is_never_mirrored_twice(self):
        message = self.emit("a-seo", "Once only")
        self.bridge.mirror_message(message["id"])
        self.bridge.flush()
        self.assertEqual(len(self.fake.sent()), 1)

    def test_secrets_are_redacted_in_mirrored_text(self):
        with patch.object(self.vault, "redact", lambda text: str(text).replace("SECRET-VALUE", "[redacted]")):
            self.bridge.say(CHAT, "key SECRET-VALUE")
        self.bridge.flush()
        self.assertEqual(self.fake.texts(), ["key [redacted]"])


class FileTests(TelegramBase):
    def setUp(self):
        super().setUp()
        self.link()
        self.run_record = self.engine.chat({"agent_id": "a-seo", "message": "Make files", "channel": "dm-a-seo"})
        self.actor = self.store.get("agents", "a-seo")
        self.fake.clear()
        self.bridge.run_jobs(force=True)
        self.bridge.flush()
        self.fake.clear()

    def make(self, name, data, deliverable=True):
        path = self.engine.workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return self.engine.outputs.register({"path": name, "title": name, **({"deliverable": True} if deliverable else {})}, self.actor, self.run_record)

    def test_plan_by_size_and_type(self):
        mb = 1024 * 1024
        self.assertEqual(tg.file_plan(2 * mb, ".PNG"), "photo")
        self.assertEqual(tg.file_plan(10 * mb, ".jpg"), "photo")
        self.assertEqual(tg.file_plan(10 * mb + 1, ".jpg"), "document")
        self.assertEqual(tg.file_plan(mb, ".pdf"), "document")
        self.assertEqual(tg.file_plan(50 * mb, ".zip"), "document")
        self.assertEqual(tg.file_plan(50 * mb + 1, ".zip"), "note")
        self.assertEqual(tg.file_plan(50 * mb + 1, ".png"), "note")

    def test_deliverable_image_is_sent_as_photo_and_text_as_document_once(self):
        image = self.make("out/shot.png", PNG)
        document = self.make("out/notes.md", "# Notes")
        self.bridge.run_jobs(force=True)
        self.bridge.flush()
        photo, doc = self.fake.sent("sendPhoto"), self.fake.sent("sendDocument")
        self.assertEqual(len(photo), 1)
        self.assertEqual(len(doc), 1)
        calls = {m: f for m, _, f in self.fake.calls if f}
        self.assertEqual((calls["sendPhoto"][0], calls["sendPhoto"][1], calls["sendPhoto"][2]), ("photo", "shot.png", PNG))
        self.assertEqual((calls["sendDocument"][0], calls["sendDocument"][1]), ("document", "notes.md"))
        self.assertEqual(photo[0]["chat_id"], str(CHAT))
        self.assertEqual(self.bridge.send_artifact(image["id"]), "later")   # same file again: paused, not repeated
        self.bridge.run_jobs(force=True)
        self.bridge.flush()
        self.assertEqual(len(self.fake.sent("sendPhoto")), 1)
        self.assertEqual(document["id"] in self.bridge.file_sent_at, True)

    def test_oversized_file_becomes_a_text_note(self):
        item = self.make("out/big.bin.txt", "x" * 200)
        self.fake.clear()
        with patch.object(tg, "DOCUMENT_LIMIT", 100), patch.object(tg, "PHOTO_LIMIT", 50):
            self.assertEqual(self.bridge.send_artifact(item["id"], force=True), "note")
        self.bridge.flush()
        self.assertEqual(self.fake.sent("sendDocument"), [])
        self.assertIn("too large to send on Telegram", self.fake.texts()[0])
        self.assertIn("big.bin.txt", self.fake.texts()[0])

    def test_image_over_the_photo_limit_goes_as_document(self):
        item = self.make("out/wide.png", PNG)
        self.fake.clear()
        with patch.object(tg, "PHOTO_LIMIT", 10):
            self.assertEqual(self.bridge.send_artifact(item["id"], force=True), "document")
        self.bridge.flush()
        self.assertEqual(len(self.fake.sent("sendDocument")), 1)
        self.assertEqual(self.fake.sent("sendPhoto"), [])

    def test_rejected_photo_falls_back_to_a_document(self):
        item = self.make("out/odd.png", PNG)
        self.fake.clear()
        self.fake.script["sendPhoto"] = [TelegramError(400, "Bad Request: IMAGE_PROCESS_FAILED")]
        self.bridge.send_artifact(item["id"], force=True)
        self.bridge.flush()
        self.bridge.flush()
        self.assertEqual(len(self.fake.sent("sendDocument")), 1)

    def test_files_switch_stops_automatic_sending_but_not_requested_files(self):
        self.service.settings({"telegram": {"forward": {"files": False}}})
        item = self.make("out/private.txt", "hello")
        self.bridge.run_jobs(force=True)
        self.bridge.flush()
        self.assertEqual(self.fake.sent("sendDocument"), [])
        self.bridge.send_artifact(item["id"], force=True)
        self.bridge.flush()
        self.assertEqual(len(self.fake.sent("sendDocument")), 1)

    def test_message_attachments_are_sent_after_the_text(self):
        item = self.make("out/report.txt", "report", deliverable=False)
        self.fake.clear()
        self.engine.team.emit("a-seo", "Report attached", self.run_record, "chat", artifact_ids=[item["id"]])
        self.bridge.run_jobs(force=True)
        self.bridge.flush()
        methods = [m for m, _, _ in self.fake.calls if m in ("sendMessage", "sendDocument")]
        self.assertEqual(methods[0], "sendMessage")
        self.assertIn("sendDocument", methods)

    def test_missing_file_is_reported_instead_of_crashing(self):
        item = self.make("out/gone.txt", "bye")
        (self.engine.workspace / "out" / "gone.txt").unlink()
        self.fake.clear()
        self.bridge.send_artifact(item["id"], force=True)
        self.bridge.flush()
        self.assertIn("not available to send", self.fake.texts()[0])


class OutboxTests(TelegramBase):
    def setUp(self):
        super().setUp()
        self.link()

    def item(self):
        return self.store.all("channel_outbox")[-1]

    def test_rate_limit_pauses_the_queue_and_keeps_order(self):
        self.fake.script["sendMessage"] = [TelegramError(429, "Too Many Requests", retry_after=3)]
        self.bridge.say(CHAT, "one")
        self.bridge.say(CHAT, "two")
        self.assertEqual(self.bridge.flush(), 0)
        self.assertGreater(self.bridge.cooldown_until, time.time() + 1)
        self.assertEqual(self.bridge.flush(), 0)
        self.assertEqual(len(self.fake.sent()), 1)
        self.bridge.cooldown_until = 0
        self.assertEqual(self.bridge.flush(), 2)
        self.assertEqual(self.fake.texts()[1:], ["one", "two"])

    def test_temporary_failure_is_retried_with_backoff_then_sent(self):
        self.fake.script["sendMessage"] = [TelegramError(500, "Internal Server Error")]
        self.bridge.say(CHAT, "retry me")
        self.bridge.flush()
        queued = self.item()
        self.assertEqual((queued["status"], queued["attempts"]), ("queued", 1))
        self.assertGreater(queued["retry_at"], time.time())
        self.store.patch("channel_outbox", queued["id"], {"retry_at": 0})
        self.bridge.flush()
        self.assertEqual(self.item()["status"], "sent")
        self.assertEqual(self.fake.texts()[-1], "retry me")

    def test_permanent_failure_is_recorded_and_does_not_block_the_queue(self):
        self.fake.script["sendMessage"] = [TelegramError(403, "Forbidden: bot was kicked from the supergroup chat")]
        self.bridge.say(CHAT, "lost")
        self.bridge.say(CHAT, "next")
        self.bridge.flush()
        statuses = [i["status"] for i in self.store.all("channel_outbox")]
        self.assertEqual(statuses, ["failed", "sent"])
        self.assertEqual(len(self.events("Telegram message not delivered")), 1)

    def test_bad_html_is_resent_as_plain_text(self):
        self.fake.script["sendMessage"] = [TelegramError(400, "Bad Request: can't parse entities: Unsupported start tag")]
        self.bridge.say(CHAT, "x <b>y</b>", html="x <b>y</b>")
        self.bridge.flush()
        self.bridge.flush()
        last = self.fake.sent()[-1]
        self.assertNotIn("parse_mode", last)
        self.assertEqual(last["text"], "x y")

    def test_unauthorized_while_sending_halts_the_bridge(self):
        self.fake.script["sendMessage"] = [TelegramError(401, "Unauthorized")]
        self.bridge.say(CHAT, "hello")
        self.bridge.flush()
        self.assertEqual(self.bridge.halted, "401")
        self.assertEqual(self.item()["status"], "queued")

    def test_pending_messages_survive_a_restart(self):
        self.bridge.say(CHAT, "before restart")
        replacement = tg.TelegramBridge(self.engine)
        replacement.http, replacement.token = self.fake.call, lambda: FAKE_TOKEN
        replacement.restore_outbox()
        replacement.flush()
        self.assertEqual(self.fake.texts(), ["before restart"])

    def test_the_regular_dispatcher_leaves_group_messages_to_the_bridge(self):
        self.bridge.say(CHAT, "mine")
        self.engine.dispatch_outbox()
        self.assertEqual(self.item()["status"], "queued")
        self.assertEqual(self.fake.sent(), [])

    def test_test_message_requires_a_running_linked_bridge(self):
        with self.assertRaises(DomainError):
            self.bridge.test()   # not started: nothing is polling
        alive = threading.Thread(target=lambda: None)
        alive.start()
        alive.join()
        self.bridge.threads = [alive]   # something "running"; close() can join it safely
        self.bridge.test()
        self.bridge.flush()
        self.assertEqual(self.fake.sent()[0]["text"], self.bridge.hello_html())
        self.assertIn(tg.HELLO["en"], self.fake.texts()[0])
        self.store.settings({"telegram": {}})
        with self.assertRaises(DomainError):
            self.bridge.test()


class CommandTests(TelegramBase):
    def setUp(self):
        super().setUp()
        self.link()

    def last(self, text, **kwargs):
        return "\n".join(self.say_out(text, **kwargs))

    def test_help_lists_every_command_and_aliases_work(self):
        text = self.last("/help")
        for command in ("/status", "/agents", "/files", "/goal", "/stop", "/approve", "/showall", "/get", "/set", "/restart", "/unlink"):
            self.assertIn(command, text)
        self.assertIn("/showall", self.last("/HELP"))
        self.assertIn("== Team ==", self.last("/Show-All"))
        self.assertIn("== Team ==", self.last("/showall@af_test_bot"))
        self.assertEqual(self.say_out("/showall@SomeOtherBot"), [])

    def test_unknown_slash_commands_go_to_the_agent_as_chat(self):
        with patch.object(self.engine, "chat", return_value={}) as chat:
            self.assertEqual(self.send("/model gpt"), "routed:a-lead")
        self.assertEqual(chat.call_args[0][0]["message"], "/model gpt")

    def test_agents_lists_the_roster(self):
        text = self.last("/agents")
        for name in ("Atlas", "Nova", "Luna", "Pixel", "Echo"):
            self.assertIn(name, text)

    def test_showall_groups_every_setting_with_value_and_type(self):
        self.fake.clear()
        self.send("/showall")
        text = "\n".join(self.fake.texts())
        for group in ("Team", "Models", "Budget", "Heartbeat", "Approvals", "Telegram", "Housekeeping", "Images"):
            self.assertIn("== %s ==" % group, text)
        self.assertIn("max_parallel = 4  [whole number 1-8]", text)
        self.assertIn("execution_enabled = on  [on/off]", text)
        self.assertIn("quota_policy = ask  [selection]", text)
        self.assertIn("workspace_name = ", text)
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", text)
        self.assertTrue(all(len(p["text"]) <= 4096 for p in self.fake.sent()))

    def test_get_shows_value_type_and_choices(self):
        text = self.last("/get quota_policy")
        self.assertIn("quota_policy = ask", text)
        self.assertIn("Type: selection", text)
        self.assertIn("Choices: ask, auto_fallback, wait", text)
        self.assertIn("Type: whole number 1-8", self.last("/get MAX_PARALLEL"))

    def test_get_unknown_key_suggests_close_ones_and_secrets_are_refused(self):
        self.assertIn("Did you mean max_parallel", self.last("/get max_paralel"))
        self.assertIn("Unknown setting", self.last("/get nonsense"))
        for key in ("TELEGRAM_BOT_TOKEN", "openai_api_key", "webhook_secret", "password"):
            self.assertIn("never shown or changed from Telegram", self.last("/get " + key))
            self.assertIn("never shown or changed from Telegram", self.last("/set %s abc" % key))

    def test_set_text_enforces_length_and_stores_old_and_new(self):
        old = self.store.settings().get("workspace_name", "")
        self.assertIn("at most 120 characters (you sent 121)", self.last("/set workspace_name " + "x" * 121))
        self.assertEqual(self.store.settings().get("workspace_name", ""), old)
        reply = self.last("/set workspace_name  Studio  Nine ")
        self.assertEqual(reply, "workspace_name: %s -> Studio  Nine" % (old or "(empty)"))
        self.assertEqual(self.store.settings()["workspace_name"], "Studio  Nine")
        change = [e for e in self.store.all("events") if e["title"] == "Setting changed from Telegram"][-1]
        self.assertIn("workspace_name:", change["detail"])
        self.assertEqual(change["type"], "settings")
        self.assertIn("needs a value", self.last("/set workspace_name   "))

    def test_set_number_range_integer_and_locale(self):
        self.assertEqual(self.last("/set max_parallel 3"), "max_parallel: 4 -> 3")
        self.assertIn("from 1 to 8", self.last("/set max_parallel 99"))
        self.assertIn("from 1 to 8", self.last("/set max_parallel 0"))
        self.assertIn("whole number", self.last("/set max_parallel 2.5"))
        self.assertIn("whole number", self.last("/set max_parallel 2,5"))
        self.assertIn("Enter a number", self.last("/set max_parallel many"))
        self.assertIn("Enter a number", self.last("/set max_parallel 1e1"))
        self.assertEqual(self.store.settings()["max_parallel"], 3)
        self.assertTrue(self.last("/set housekeeping.min_free_gb 2,5").endswith("-> 2.5"))
        self.assertIn("housekeeping.min_free_gb: 2.5 -> 7.25", self.last("/set housekeeping.min_free_gb 7.25"))
        self.assertIn("needs a value", self.last("/set max_parallel"))

    def test_set_budget_goes_through_the_budget_validator(self):
        self.assertIn("from 1 to 500", self.last("/set budget.run_turns 501"))
        self.assertIn("budget.run_turns: 24 -> 30", self.last("/set budget.run_turns 30"))
        self.assertEqual(self.store.settings()["budget"]["run_turns"], 30)
        self.assertIn("budget.project_tokens: 0 -> 1000000", self.last("/set budget.project_tokens 1,000,000"))

    def test_set_selection_without_value_shows_keyboard_and_with_value_matches_exactly(self):
        self.fake.clear()
        self.send("/set quota_policy")
        params = self.fake.sent()[-1]
        self.assertEqual([b["text"] for b in self.buttons(params)], ["ask", "auto_fallback", "wait"])
        self.assertIn("quota_policy (now ask)", params["text"])
        self.assertEqual(self.last("/set quota_policy WAIT"), "quota_policy: ask -> wait")
        reply = self.last("/set quota_policy wai")
        self.assertIn("Choose one of the listed values.", reply)
        self.assertIn("Choices: ask, auto_fallback, wait", reply)
        self.assertEqual(self.store.settings()["quota_policy"], "wait")

    def test_selection_button_applies_the_value_and_edits_the_message(self):
        self.fake.clear()
        self.send("/set model_autonomy")
        params = self.fake.sent()[-1]
        self.fake.clear()
        self.press(params, "phases")
        self.assertEqual(self.store.settings()["model_autonomy"], "phases")
        edit = self.fake.sent("editMessageText")[0]
        self.assertEqual(edit["text"], "model_autonomy: locked -&gt; phases")
        self.assertEqual(edit["reply_markup"], {"inline_keyboard": []})

    def test_set_bool_accepts_words_and_rejects_unclear_text(self):
        self.fake.clear()
        self.send("/set adaptive_effort")
        self.assertEqual([b["text"] for b in self.buttons(self.fake.sent()[-1])], ["on", "off"])
        self.assertEqual(self.last("/set adaptive_effort Yes"), "adaptive_effort: off -> on")
        self.assertEqual(self.last("/set adaptive_effort off"), "adaptive_effort: on -> off")
        reply = self.last("/set adaptive_effort maybe")
        self.assertIn("Choose on or off.", reply)
        self.assertIn("Choices: on, off", reply)

    def test_set_text_pattern_is_enforced(self):
        self.assertIn("not a valid value", self.last("/set heartbeat.active_hours.start 25:00"))
        self.assertIn("not a valid value", self.last("/set heartbeat.active_hours.start 8"))
        self.assertIn("->", self.last("/set heartbeat.active_hours.start 08:30"))
        self.assertEqual(self.store.settings()["heartbeat"]["active_hours"]["start"], "08:30")

    def test_set_model_uses_the_catalog_and_records_an_owner_command(self):
        models = [{"provider": "codex", "id": "gpt-test-a", "name": "GPT Test A", "resolved_model": "gpt-test-a"},
                  {"provider": "claude", "id": "claude-test-b", "name": "Claude Test B", "resolved_model": "claude-test-b"}]
        with patch.object(self.providers, "models", models):
            self.fake.clear()
            self.send("/set default_model")
            self.assertEqual([b["text"] for b in self.buttons(self.fake.sent()[-1])], ["GPT Test A", "Claude Test B"])
            self.assertIn("catalog", self.last("/set default_model bogus-model"))
            self.assertIn("default_model:", self.last("/set default_model claude:claude-test-b"))
        event = [e for e in self.store.all("model_events") if e["kind"] == "team_default"][-1]
        self.assertEqual(event["source"], "owner_command")
        self.assertEqual(self.store.settings()["default_model"], "claude-test-b")

    def test_set_without_a_key_and_unknown_key(self):
        self.assertIn("Use /set <key> <value>", self.last("/set"))
        self.assertIn("Unknown setting", self.last("/set not_a_key 5"))

    def test_set_never_touches_the_vault_or_echoes_the_token(self):
        self.last("/set TELEGRAM_BOT_TOKEN " + FAKE_TOKEN)
        self.last("/get TELEGRAM_BOT_TOKEN")
        self.assertNotIn(FAKE_TOKEN, json.dumps(self.fake.calls))
        self.assertNotIn(FAKE_TOKEN, json.dumps(self.store.all("events")))

    def test_approve_reads_and_sets_the_chat_approval_mode(self):
        self.assertIn("ask", self.last("/approve"))
        self.assertIn("ask -> auto", self.last("/approve auto"))
        self.assertEqual(self.engine.approvals.modes()["dm-a-lead"], "auto")
        self.assertIn("ask -> ", self.last("/approve ask").replace("auto -> ask", "ask -> "))
        self.assertEqual(self.engine.approvals.modes()["dm-a-lead"], "ask")
        self.assertIn("Use /approve ask or /approve auto", self.last("/approve sometimes"))

    def test_goal_creates_a_project_goal(self):
        self.assertIn("Goal added to project", self.last("/goal Launch the beta in May"))
        self.assertIn("Use /goal", self.last("/goal"))
        self.assertTrue(any("Launch the beta" in json.dumps(p) for p in self.store.all("projects")))

    def test_stop_asks_to_confirm_then_cancels_the_agents_runs(self):
        self.ready("a-code")
        run = self.engine.chat({"message": "Long task", "agent_id": "a-code", "channel": "dm-a-code"})
        self.fake.clear()
        self.send("/stop pixel")
        params = self.fake.sent()[-1]
        self.assertIn("Stop all work of Pixel?", params["text"])
        self.assertEqual([b["text"] for b in self.buttons(params)], ["Confirm", "Cancel"])
        self.assertNotEqual(self.store.get("runs", run["id"])["status"], "cancelled")
        self.fake.clear()
        self.press(params, "Confirm")
        self.assertEqual(self.store.get("runs", run["id"])["status"], "cancelled")
        self.assertIn("Stopped", self.fake.sent("editMessageText")[0]["text"])
        self.assertIn("No teammate is called 'nobody'", self.last("/stop nobody"))

    def test_stop_all_and_cancel_buttons(self):
        self.fake.clear()
        self.send("/stop all")
        params = self.fake.sent()[-1]
        self.assertIn("Stop ALL", params["text"])
        with patch.object(self.engine.safety, "stop_all") as stop_all:
            self.press(params, "Cancel")
            stop_all.assert_not_called()
            self.send("/stop")
            self.press(self.fake.sent()[-1], "Confirm")
            stop_all.assert_called_once()
        self.assertIn("Execution is off", self.fake.sent("editMessageText")[-1]["text"])

    def test_restart_asks_to_confirm_then_requests_the_restart(self):
        self.fake.clear()
        self.send("/restart")
        params = self.fake.sent()[-1]
        with patch.object(self.engine, "request_restart") as request:
            self.press(params, "Cancel")
            request.assert_not_called()
            self.send("/restart")
            self.press(self.fake.sent()[-1], "Confirm")
            request.assert_called_once_with("Telegram /restart")
        self.assertIn("Restarting AgentFlow", self.fake.sent("editMessageText")[-1]["text"])

    def test_unlink_asks_to_confirm_then_unlinks(self):
        self.fake.clear()
        self.send("/unlink")
        params = self.fake.sent()[-1]
        self.press(params, "Cancel")
        self.assertTrue(self.bridge.linked())
        self.send("/unlink")
        self.press(self.fake.sent()[-1], "Confirm")
        self.assertFalse(self.bridge.linked())
        self.assertEqual(self.store.settings()["telegram_chats"], "")
        self.fake.clear()
        self.assertEqual(self.send("anyone?"), "asked")   # the chat is a stranger again and needs a new approval

    def test_files_lists_deliverables_with_send_buttons(self):
        self.assertIn("No deliverables yet", self.last("/files"))
        run = self.engine.chat({"agent_id": "a-seo", "message": "Make files", "channel": "dm-a-seo"})
        actor = self.store.get("agents", "a-seo")
        names = []
        for number in range(3):
            name = "deliver/file%d.txt" % number
            path = self.engine.workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("content %d" % number, encoding="utf-8")
            self.engine.outputs.register({"path": name, "title": "File %d" % number, "deliverable": True}, actor, run)
            names.append(name)
        self.fake.clear()
        self.send("/files 2")
        params = self.fake.sent()[-1]
        self.assertEqual(len(self.buttons(params)), 2)
        self.assertIn("1. File 2", params["text"])
        self.assertIn("Use /files or /files <1-10>.", self.last("/files 0"))
        self.fake.clear()
        self.press(params, "Send file2.txt")
        self.assertEqual(len(self.fake.sent("sendDocument")), 1)

    def test_status_shows_every_member_with_task_model_activity_queue_and_quota(self):
        self.ready("a-code")
        run = self.engine.chat({"message": "Fix the login redirect bug", "agent_id": "a-code", "channel": "dm-a-code"})
        self.store.patch("runs", run["id"], {"status": "running", "last_activity_at": tg.now()})
        self.engine.enqueue("a-seo", "Queued keyword research", "peer")
        usage = {"windows": [{"window_minutes": 300, "used_percent": 41.6, "resets_at": time.time() + 7200},
                             {"window_minutes": 10080, "used_percent": 12.0, "resets_at": time.time() + 3 * 86400}]}
        with patch.object(self.engine.providers.usage, "public", side_effect=lambda provider: usage if provider == "codex" else {"windows": []}):
            self.fake.clear()
            self.send("/status")
        text = self.fake.texts()[0]
        for name in ("Atlas", "Nova", "Luna", "Milo", "Sage", "Aria", "Pixel", "Echo"):
            self.assertIn(name, text)
        pixel = next(line for line in text.split("\n\n")[0].split("\n") if "Pixel" in line)
        self.assertIn("🔵", pixel)
        self.assertIn("Working", pixel)
        self.assertIn("Fix the login redirect bug", pixel)
        self.assertIn("Fix the login redirect bug", text)
        self.assertIn("gateway:observed-test-model", text)
        self.assertIn("active just now", text)
        nova = text.split("Nova", 1)[1].split("\n", 2)
        self.assertIn("queued 1", nova[1] + nova[2])
        self.assertIn("Quota", text)
        self.assertIn("Codex: 5h 42% used, resets in 2 h | 7d 12% used, resets in 3 d", text)
        self.assertNotIn("Claude:", text)
        self.assertTrue(self.fake.sent()[0]["text"].count("<b>") >= 8)


class RestartTests(TelegramBase):
    def test_request_restart_starts_the_helper_then_shuts_down_gracefully(self):
        self.link()
        done = threading.Event()
        self.engine.on_restart = done.set
        self.engine.http_port = 8123
        with patch.object(self.engine, "spawn") as spawn:
            self.assertEqual(self.engine.request_restart("Testing restart", delay=0), {"restarting": True})
            self.assertTrue(done.wait(5))
            self.assertEqual(self.engine.request_restart("again", delay=0), {"restarting": True, "already_requested": True})
        spawn.assert_called_once()
        command = spawn.call_args[0][0]
        self.assertEqual(command[0], sys.executable)
        self.assertTrue(command[1].endswith("restart_helper.py"))
        self.assertEqual(command[command.index("--pid") + 1], str(os.getpid()))
        self.assertEqual(command[command.index("--port") + 1], "8123")
        self.assertEqual(command[command.index("--data-dir") + 1], str(self.store.directory))
        self.assertEqual(command[command.index("--") + 1], sys.executable)   # the exact interpreter starts the server again
        self.assertTrue(Path(command[command.index("--env") + 1]).is_file())
        self.assertEqual(spawn.call_args[1]["stdout"], -3)
        self.assertEqual(len(self.events("AgentFlow restart requested")), 1)
        self.assertEqual(self.bridge.state()["restart"]["reason"], "Testing restart")

    def test_failed_launch_allows_another_attempt_and_does_not_shut_down(self):
        done = threading.Event()
        self.engine.on_restart = done.set
        with patch.object(self.engine, "spawn", side_effect=OSError("no powershell")):
            self.engine.request_restart("x", delay=0)
            deadline = time.time() + 5
            while not self.events("AgentFlow restart failed") and time.time() < deadline:
                time.sleep(0.05)
        self.assertFalse(done.is_set())
        self.assertFalse(self.engine.restart_requested)
        self.assertTrue(self.events("AgentFlow restart failed"))

    def test_wmi_launch_goes_through_one_powershell_line_and_falls_back_to_a_direct_child(self):
        self.engine.http_port = 8123
        proc = MagicMock()
        proc.wait.return_value = 0
        with patch.object(self.engine, "spawn", return_value=proc) as spawn:
            self.engine.launch_restarter(use_cim=True)
        spawn.assert_called_once()
        command = spawn.call_args[0][0]
        self.assertEqual(command[0], "powershell.exe")
        script = command[command.index("-Command") + 1]
        self.assertIn("Invoke-CimMethod -ClassName Win32_Process -MethodName Create", script)
        self.assertIn("restart_helper.py", script)
        self.assertIn("--pid %d --port 8123" % os.getpid(), script)
        failed = MagicMock()
        failed.wait.return_value = 3   # WMI refused
        with patch.object(self.engine, "spawn", side_effect=[failed, MagicMock()]) as spawn:
            self.engine.launch_restarter(use_cim=True)
        self.assertEqual(spawn.call_count, 2)
        direct = spawn.call_args_list[1][0][0]
        self.assertEqual(direct[0], sys.executable)
        self.assertTrue(direct[1].endswith("restart_helper.py"))
        self.assertTrue(self.events("AgentFlow restart helper"))

    def test_environment_file_for_the_helper_never_holds_secrets(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "OPENAI_API_KEY": "sk-fake", "PYENV_ROOT": "C:\\pyenv", "CODEX_PATH": "C:\\codex.cmd"}):
            command = self.engine.restart_command()
        text = Path(command[command.index("--env") + 1]).read_text(encoding="utf-8")
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertNotIn("sk-fake", text)
        self.assertIn("PYENV_ROOT", text)
        self.assertIn("CODEX_PATH", text)

    @unittest.skipIf(os.name == "nt", "the real self-restart is available on Windows")
    def test_restart_is_refused_cleanly_where_it_cannot_work(self):
        with self.assertRaises(DomainError) as caught:
            self.engine.request_restart("x")
        self.assertEqual(caught.exception.status, 501)

    def test_script_hands_over_to_the_helper_and_stops_the_runtime(self):
        script = self.engine.RESTART_SCRIPT.read_text(encoding="utf-8")
        for needle in ("Win32_Process", "Invoke-CimMethod", "restart_helper.py", "Start-AgentFlow.ps1", "-NoBrowser", "Stop-AgentFlow.ps1",
                       "$ProcessId", "$Port", "Resolve-AgentFlowPython", "--data-dir", "--env"):
            self.assertIn(needle, script)
        script.encode("ascii")

    def test_script_finds_the_runtime_itself_and_starts_it_when_none_runs(self):
        script = self.engine.RESTART_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("param([int]$ProcessId = 0,", script)
        self.assertNotIn("Mandatory", script)
        for needle in ("Get-NetTCPConnection -LocalPort $Port -State Listen", "Win32_Process", "server.py", "python.exe"):
            self.assertIn(needle, script)
        self.assertRegex(script, r"(?s)if \(\$ProcessId -le 0\).*?\$startScript.*?-NoBrowser.*?return")

    def test_double_clickable_cmd_runs_the_script_and_checks_the_port(self):
        raw = (self.engine.RESTART_SCRIPT.parents[1] / "Restart-AgentFlow.cmd").read_bytes()
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))   # CRLF only
        text = raw.decode("ascii")
        self.assertIn('powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\\Restart-AgentFlow.ps1"', text)
        self.assertIn("http://127.0.0.1:%PORT%", text)
        self.assertIn("AgentFlow restarted", text)
        self.assertEqual(text.count("pause"), 1)
        self.assertLess(text.index("ERROR"), text.index("pause"))

    def test_next_start_announces_the_restart_in_web_chat_and_telegram(self):
        self.link()
        self.bridge.note_restart("Testing")
        self.bridge.announce_restart()
        self.bridge.run_jobs(force=True)   # the web copy must not be mirrored a second time
        self.bridge.flush()
        text = self.fake.texts()[-1]
        self.assertRegex(text, r"^AgentFlow restarted \(v[0-9.]+(/[0-9a-f]+)?\) - took \d+s - Telegram connected as @af_test_bot$")
        web = [m for m in self.store.all("messages") if "AgentFlow restarted" in m["content"]]
        self.assertEqual(len(web), 1)
        self.assertEqual(web[0]["audience"], "owner")
        self.bridge.announce_restart()   # only once per restart
        self.bridge.flush()
        self.assertEqual(len([t for t in self.fake.texts() if "AgentFlow restarted" in t]), 1)

    def test_next_start_reports_a_failed_restart_once_and_removes_the_file(self):
        self.link()
        failure = Path(self.store.directory) / "restart-failed.json"
        failure.write_text(json.dumps({"at": "2026-01-01T00:00:00Z", "reason": "The server did not answer on port 8765 after 4 attempts.", "log_tail": "x"}))
        self.bridge.announce_restart()
        self.bridge.flush()
        self.assertFalse(failure.exists())
        texts = [t for t in self.fake.texts() if "restart did not complete" in t]
        self.assertEqual(len(texts), 1)
        self.assertIn("did not answer on port 8765", texts[0])
        self.assertIn("restart.log", texts[0])
        self.assertEqual(len([m for m in self.store.all("messages") if "restart did not complete" in m["content"]]), 1)
        self.bridge.announce_restart()
        self.bridge.flush()
        self.assertEqual(len([t for t in self.fake.texts() if "restart did not complete" in t]), 1)

    def test_normal_start_does_not_announce_anything(self):
        self.link()
        self.bridge.announce_restart()
        self.bridge.flush()
        self.assertEqual(self.fake.sent(), [])


class ReloadTests(TelegramBase):
    """A saved, replaced or removed token and a changed mode apply at once, without restarting the runtime."""

    def setUp(self):
        super().setUp()
        self.holder = {"token": ""}
        self.bridge.token = lambda: self.holder["token"]
        self.addCleanup(self.bridge.close)

    def pollers(self):
        return [t for t in threading.enumerate() if t.name == "agentflow-telegram-poll" and t.is_alive()]

    def wait_for(self, check):
        deadline = time.time() + 5
        while not check() and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(check())

    def test_new_token_connects_and_starts_polling(self):
        self.assertFalse(self.bridge.start())
        self.holder["token"] = FAKE_TOKEN
        status = self.bridge.reload()
        self.assertEqual([m for m, _, _ in self.fake.calls][:3], ["getMe", "deleteWebhook", "setMyCommands"])
        self.assertTrue(status["connected"] and status["configured"])
        self.assertEqual(status["username"], "af_test_bot")
        self.wait_for(lambda: self.fake.sent("getUpdates"))
        self.assertEqual(len(self.pollers()), 1)
        self.assertEqual(len(self.fake.sent("getMe")), 1)   # the poller does not repeat the setup

    def test_replaced_token_stops_the_old_poller_and_starts_a_new_one(self):
        self.holder["token"] = FAKE_TOKEN
        self.bridge.reload()
        first = self.pollers()
        self.assertEqual(len(first), 1)
        self.holder["token"] = FAKE_TOKEN.replace("123456", "654321")
        self.bridge.reload()
        self.wait_for(lambda: len(self.pollers()) == 1 and self.pollers()[0] not in first)
        self.assertEqual(self.fake.tokens, {FAKE_TOKEN, FAKE_TOKEN.replace("123456", "654321")})
        self.assertEqual(self.bridge.generation, 2)

    def test_stale_poller_discards_updates_and_ignores_its_errors(self):
        self.link()
        stale = self.bridge.generation
        self.bridge.generation += 1
        self.fake.updates = [self.message("old poller")]
        with patch.object(self.engine, "chat") as chat:
            self.assertEqual(self.bridge.poll_once(generation=stale), 0)
        chat.assert_not_called()
        self.assertNotIn("offset", self.bridge.state())
        self.fake.script["getUpdates"] = [TelegramError(409, "Conflict")]
        self.bridge.poll_loop(stale)
        self.assertEqual(self.bridge.halted, "")

    def test_removed_token_stops_telegram(self):
        self.holder["token"] = FAKE_TOKEN
        self.bridge.reload()
        with patch.object(self.vault, "set", side_effect=lambda values, replace_prefix=None: self.holder.update(token="")) as saved:
            result = self.bridge.clear_token()
        self.assertEqual(saved.call_args[1], {"replace_prefix": "TELEGRAM_BOT_TOKEN"})
        self.assertFalse(result["from_environment"])
        self.assertFalse(result["telegram"]["configured"] or result["telegram"]["connected"])
        self.assertEqual(result["telegram"]["username"], "")
        self.wait_for(lambda: not self.pollers())
        self.assertEqual(self.bridge.threads, [])

    def test_rejected_token_is_reported_and_a_good_one_recovers(self):
        self.holder["token"] = FAKE_TOKEN
        self.fake.script["getMe"] = [TelegramError(401, "Unauthorized")]
        status = self.bridge.reload()
        self.assertEqual((status["halted"], status["connected"], status["last_error"]), ("401", False, "Unauthorized"))
        self.assertEqual(self.bridge.threads, [])
        self.assertEqual(len(self.events("Telegram token rejected")), 1)
        status = self.bridge.reload()
        self.assertEqual((status["halted"], status["connected"], status["last_error"]), ("", True, ""))
        self.wait_for(lambda: self.fake.sent("getUpdates"))

    def test_unreachable_telegram_still_starts_a_retrying_poller(self):
        self.holder["token"] = FAKE_TOKEN
        self.fake.script["getMe"] = [TelegramError(0, "Cannot reach Telegram")] * 2   # the reload and the poller's first try
        status = self.bridge.reload()
        self.assertFalse(status["connected"])
        self.wait_for(lambda: self.bridge.state().get("connected"))   # the poller's own backoff reconnects
        self.assertEqual(len(self.pollers()), 1)

    def test_switching_to_webhook_keeps_the_webhook_and_stops_polling(self):
        self.holder["token"] = FAKE_TOKEN
        self.bridge.reload()
        self.fake.clear()
        self.store.settings({"telegram_mode": "webhook"})
        status = self.bridge.reload()
        self.assertEqual(status["mode"], "webhook")
        self.assertEqual(self.fake.sent("deleteWebhook"), [])
        self.wait_for(lambda: not self.pollers())
        self.assertEqual([t.name for t in self.bridge.threads], ["agentflow-telegram-out"])
        self.store.settings({"telegram_mode": "polling"})
        self.bridge.reload()
        self.assertEqual(len(self.pollers()), 1)
        self.assertEqual(len(self.fake.sent("deleteWebhook")), 1)

    def test_queued_messages_are_still_sent_after_a_reload(self):
        self.link()
        self.holder["token"] = FAKE_TOKEN
        self.bridge.reload()
        self.bridge.say(CHAT, "after the reload")
        self.bridge.reload()
        self.wait_for(lambda: "after the reload" in self.fake.texts())
        self.assertEqual(self.fake.texts().count("after the reload"), 1)

    def request(self, path, body):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service, "session", 0))
        port = server.server_port
        server.RequestHandlerClass = make_handler(self.service, "session", port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection("127.0.0.1", port)
        try:
            conn.request("POST", path, json.dumps(body), {"Cookie": "af_session=session", "Content-Type": "application/json", "Origin": "http://127.0.0.1:%d" % port})
            response = conn.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            conn.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_web_routes_apply_token_and_mode_changes_immediately(self):
        self.holder["token"] = FAKE_TOKEN
        with patch.object(self.vault, "set", return_value={"saved": True, "persistence": "test"}) as saved:
            status, body = self.request("/api/secrets", {"TELEGRAM_BOT_TOKEN": FAKE_TOKEN})
        self.assertEqual(status, 200)
        self.assertEqual(saved.call_args[0][0], {"TELEGRAM_BOT_TOKEN": FAKE_TOKEN})
        self.assertTrue(body["telegram"]["connected"])
        self.assertEqual(body["telegram"]["username"], "af_test_bot")
        self.assertNotIn(FAKE_TOKEN, json.dumps(body))
        with patch.object(self.vault, "set") as untouched:
            status, body = self.request("/api/secrets", {"TELEGRAM_BOT_TOKEN": "not a token"})
        self.assertEqual(status, 400)
        self.assertIn("does not look like a Telegram bot token", body["error"])
        self.assertNotIn("not a token", body["error"])
        untouched.assert_not_called()
        with patch.object(self.bridge, "reload", wraps=self.bridge.reload) as reload:
            self.request("/api/settings", {"telegram_mode": "polling"})   # unchanged: nothing to apply
            reload.assert_not_called()
            status, body = self.request("/api/settings", {"telegram_mode": "webhook"})
            self.assertEqual(status, 200)
            reload.assert_called_once()
        self.assertEqual(self.bridge.mode(), "webhook")
        status, body = self.request("/api/telegram/token-clear", {})
        self.assertEqual((status, body["cleared"]), (200, True))


if __name__ == "__main__":
    unittest.main()
