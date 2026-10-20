import unittest
from unittest.mock import patch

import test_runtime as runtime
from agentflow import config_schema as cs
from agentflow.config_schema import ConfigError


class ParseNumberTests(unittest.TestCase):
    def test_locale_tolerant_decimals_and_grouping(self):
        cases = [("5", False, 5), ("5.0", False, 5), ("1.5", False, 1.5), ("1,5", False, 1.5), (" 2 000 ", True, 2000),
                 ("1,000", True, 1000), ("1.000", True, 1000), ("1,000,000", True, 1000000), ("1.234,5", False, 1234.5),
                 ("1,234.5", False, 1234.5), ("+7", True, 7), ("0,25", False, 0.25), ("1_000", True, 1000)]
        for text, integer, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(cs.parse_number(text, integer), expected)

    def test_garbage_is_rejected(self):
        for text in ("", "abc", "1e5", "5 apples", "--3", "1..5", "1,,5", "1.234.56", "12,34.567,8", "NaN", "inf", "0x10", "١٢"):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                cs.parse_number(text)

    def test_integer_and_range_are_enforced(self):
        for text in ("2.5", "2,5", "0.1"):
            with self.subTest(text=text), self.assertRaises(ConfigError) as caught:
                cs.parse_number(text, True, 0, 10)
            self.assertIn("whole number", str(caught.exception))
        self.assertEqual(cs.parse_number("10", True, 1, 10), 10)
        for text in ("0", "11", "-1"):
            with self.subTest(text=text), self.assertRaises(ConfigError) as caught:
                cs.parse_number(text, True, 1, 10)
            self.assertIn("from 1 to 10", str(caught.exception))
        self.assertEqual(cs.parse_number("-2,5", False, -5, 5), -2.5)


class RegistryTests(unittest.TestCase):
    def test_every_field_is_typed_and_grouped(self):
        keys = [f.key for f in cs.FIELDS]
        self.assertEqual(len(keys), len(set(keys)))
        for field in cs.FIELDS:
            self.assertIn(field.kind, cs.TYPES, field.key)
            self.assertIn(field.group, cs.GROUPS, field.key)
            self.assertTrue(field.description, field.key)
            if field.kind == "number":
                self.assertIsNotNone(field.minimum, field.key)
                self.assertIsNotNone(field.maximum, field.key)
            if field.kind in ("selection", "model"):
                self.assertIsNotNone(field.options, field.key)
        self.assertEqual({f.group for f in cs.FIELDS}, set(cs.GROUPS))

    def test_credentials_are_not_in_the_registry(self):
        for field in cs.FIELDS:
            self.assertFalse(any(word in field.key.casefold() for word in ("secret", "password", "api_key", "apikey", "credential")), field.key)
            self.assertNotIn("TELEGRAM", field.key.upper().replace("TELEGRAM_MODE", "").replace("TELEGRAM.", ""))

    def test_find_is_case_insensitive_refuses_secrets_and_suggests(self):
        self.assertEqual(cs.find("MAX_PARALLEL").key, "max_parallel")
        self.assertEqual(cs.find("budget.project_tokens").key, "budget.project_tokens")   # "tokens" is a limit, not a credential
        for key in ("TELEGRAM_BOT_TOKEN", "bot_token", "OPENAI_API_KEY", "webhook_secret", "admin password", "api-key"):
            with self.subTest(key=key), self.assertRaises(ConfigError) as caught:
                cs.find(key)
            self.assertIn("never shown or changed", str(caught.exception))
        with self.assertRaises(ConfigError) as caught:
            cs.find("max_paralel")
        self.assertIn("Did you mean max_parallel", str(caught.exception))
        with self.assertRaises(ConfigError) as caught:
            cs.find("zzzz")
        self.assertIn("/showall", str(caught.exception))
        with self.assertRaises(ConfigError):
            cs.find("  ")


class ConfigTests(unittest.TestCase):
    setUp = runtime.RuntimeTests.setUp
    tearDown = runtime.RuntimeTests.tearDown

    def parse(self, key, text):
        return cs.parse(self.engine, cs.find(key), text)

    def test_text_rules(self):
        self.assertEqual(self.parse("workspace_name", "  My studio "), "My studio")
        for text, message in (("", "cannot be empty"), ("x" * 121, "at most 120"), ("two\nlines", "control characters")):
            with self.subTest(text=text[:10]), self.assertRaises(ConfigError) as caught:
                self.parse("workspace_name", text)
            self.assertIn(message, str(caught.exception))
        self.assertEqual(self.parse("image.model", ""), "")   # this one may be cleared
        with self.assertRaises(ConfigError):
            self.parse("image.model", "bad model name!")

    def test_selection_and_bool_must_match_an_option(self):
        self.assertEqual(self.parse("quota_policy", "Auto_Fallback"), "auto_fallback")
        with self.assertRaises(ConfigError) as caught:
            self.parse("quota_policy", "auto")
        self.assertEqual([v for v, _ in caught.exception.options], ["ask", "auto_fallback", "wait"])
        for word in ("on", "TRUE", "yes", "1", "enabled"):
            self.assertIs(self.parse("execution_enabled", word), True)
        for word in ("off", "False", "no", "0", "disabled"):
            self.assertIs(self.parse("execution_enabled", word), False)
        with self.assertRaises(ConfigError) as caught:
            self.parse("execution_enabled", "perhaps")
        self.assertEqual([v for v, _ in caught.exception.options], ["on", "off"])

    def test_agent_selection_accepts_id_or_name(self):
        self.assertEqual(self.parse("telegram.default_agent_id", "a-seo"), "a-seo")
        self.assertEqual(self.parse("telegram.default_agent_id", "nova"), "a-seo")
        self.assertEqual(self.parse("heartbeat.pulse.agent_id", "Atlas (a-lead)"), "a-lead")
        with self.assertRaises(ConfigError):
            self.parse("telegram.default_agent_id", "a-nobody")

    def test_model_field_uses_the_catalog(self):
        models = [{"provider": "codex", "id": "gpt-test-a", "name": "GPT Test A", "resolved_model": "gpt-test-a"},
                  {"provider": "claude", "id": "claude-test-b", "name": "Claude Test B", "resolved_model": "claude-test-b"},
                  {"provider": "claude", "id": "gpt-test-a", "name": "Shared id", "resolved_model": "gpt-test-a"}]
        with patch.object(self.providers, "models", models):
            self.assertEqual(self.parse("default_model", "claude:claude-test-b"), "claude:claude-test-b")
            self.assertEqual(self.parse("default_model", "claude-test-b"), "claude:claude-test-b")
            with self.assertRaises(ConfigError) as caught:
                self.parse("default_model", "gpt-test-a")   # two providers have it
            self.assertIn("provider:model", str(caught.exception))
            with self.assertRaises(ConfigError) as caught:
                self.parse("default_model", "nope")
            self.assertIn("catalog", str(caught.exception))
            self.assertEqual(len(cs.options_of(self.engine, cs.find("default_model"))), 3)

    def test_apply_uses_the_service_validators_and_records_an_event(self):
        field = cs.find("max_parallel")
        self.assertEqual(cs.apply(self.engine, field, 2), ("4", "2"))
        self.assertEqual(self.store.settings()["max_parallel"], 2)
        event = [e for e in self.store.all("events") if e["title"] == "Setting changed from Telegram"][-1]
        self.assertEqual((event["detail"], event["type"]), ("max_parallel: 4 -> 2", "settings"))

    def test_apply_nested_settings_keep_their_siblings(self):
        self.assertEqual(cs.apply(self.engine, cs.find("heartbeat.pulse.every_minutes"), 45)[1], "45")
        self.assertEqual(cs.apply(self.engine, cs.find("heartbeat.pulse.notify"), "always")[1], "always")
        pulse = self.engine.heartbeat.config()["pulse"]
        self.assertEqual((pulse["every_minutes"], pulse["notify"]), (45, "always"))
        self.assertEqual(cs.apply(self.engine, cs.find("telegram.forward.files"), False)[1], "off")
        forward = self.engine.telegram.config()["forward"]
        self.assertEqual((forward["files"], forward["questions"]), (False, True))

    def test_apply_budget_rejects_values_the_validator_rejects(self):
        field = cs.find("budget.run_turns")
        with self.assertRaises(ConfigError):
            cs.apply(self.engine, field, 100000)
        self.assertEqual(cs.apply(self.engine, field, 12), ("24", "12"))
        self.assertEqual(self.engine.safety.budget()["run_turns"], 12)

    def test_apply_default_model_goes_through_set_default_as_owner_command(self):
        models = [{"provider": "codex", "id": "gpt-test-a", "name": "GPT Test A", "resolved_model": "gpt-test-a"}]
        with patch.object(self.providers, "models", models):
            with patch.object(self.engine.models, "set_default", wraps=self.engine.models.set_default) as set_default:
                old, new = cs.apply(self.engine, cs.find("default_model"), "codex:gpt-test-a")
        self.assertEqual(new, "codex:gpt-test-a")
        self.assertEqual(set_default.call_args[1]["source"], "owner_command")
        self.assertEqual([e["source"] for e in self.store.all("model_events") if e["kind"] == "team_default"][-1], "owner_command")

    def test_apply_approval_mode_for_the_telegram_agent_channel(self):
        self.assertEqual(cs.approval_channel(self.engine), "dm-a-lead")
        self.assertEqual(cs.apply(self.engine, cs.find("approvals.mode"), "auto"), ("ask", "auto"))
        self.assertEqual(self.engine.approvals.modes()["dm-a-lead"], "auto")

    def test_listing_covers_every_group_and_never_raises(self):
        groups = dict(cs.listing(self.engine))
        self.assertEqual(list(groups), list(cs.GROUPS))
        self.assertEqual(sum(len(rows) for rows in groups.values()), len(cs.FIELDS))
        row = next(r for r in groups["Team"] if r[0] == "max_parallel")
        self.assertEqual(row, ("max_parallel", "4", "whole number 1-8"))
        with patch.object(cs, "current", side_effect=RuntimeError("boom")):
            self.assertTrue(all(r[1] == "(unavailable)" for _, rows in cs.listing(self.engine) for r in rows))

    def test_every_registered_field_can_be_read(self):
        for field in cs.FIELDS:
            with self.subTest(key=field.key):
                cs.show(cs.current(self.engine, field))

    def test_every_field_accepts_its_own_current_value_where_one_is_set(self):
        # round trip: the displayed value of each selection/bool field must parse back to itself
        for field in cs.FIELDS:
            if field.kind not in ("selection", "bool"):
                continue
            value = cs.current(self.engine, field)
            if value in (None, ""):
                continue
            with self.subTest(key=field.key):
                shown = cs.show(value)
                self.assertEqual(cs.parse(self.engine, field, shown), value)


if __name__ == "__main__":
    unittest.main()
