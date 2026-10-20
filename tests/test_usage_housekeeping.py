import collections
import http.client
import io
import json
import os
import threading
import time
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import test_runtime as runtime
from agentflow import housekeeping as hk
from agentflow.accounts import Accounts, AccountChanged, stable_identity, identity_digest
from agentflow.providers import Providers
from agentflow.store import DomainError
from agentflow.usage import CLAUDE_TOKEN_NOTICE
from http.server import ThreadingHTTPServer
from server import make_handler

DAY = 86400
Usage = collections.namedtuple('Usage', 'total used free')


class UsageBase(unittest.TestCase):
    def setUp(self):
        runtime.RuntimeTests.setUp(self)
        self.profile = self.root / 'cli-profile'
        self.profile.mkdir()
        self.home_patch = patch.object(self.providers.accounts, 'home', return_value=self.profile)
        self.home_patch.start()
        self.refresh_patch = patch.object(self.providers, 'refresh_models', return_value=[])
        self.refresh_patch.start()

    def tearDown(self):
        for thread in self.providers.status_threads.values():
            thread.join(timeout=5)
        self.home_patch.stop()
        self.refresh_patch.stop()
        runtime.RuntimeTests.tearDown(self)

    def claude_files(self, token='token-one', uuid='uuid-a', email='me@example.test', expires=None):
        (self.profile / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {
            'accessToken': token, 'refreshToken': 'refresh-' + token, 'subscriptionType': 'max', 'rateLimitTier': 'tier',
            'expiresAt': expires if expires is not None else (time.time() + 3600) * 1000}}))
        (self.profile / '.claude.json').write_text(json.dumps({'oauthAccount': {
            'accountUuid': uuid, 'emailAddress': email, 'organizationUuid': 'org-1'}, 'projects': {'x': 1}}))

    def claude_account(self, email='me@example.test'):
        return {'type': 'claude.ai', 'email': email, 'org_id': 'org-1', 'subscription_type': 'max'}

    def claude_state(self):
        self.claude_files()
        with patch.object(self.providers, 'read_account', return_value=self.claude_account()):
            return self.providers.accounts.check('claude', force=True)


class ClaudeUsageTests(UsageBase):
    def test_credential_refresh_does_not_blank_usage(self):
        state = self.claude_state()
        self.providers.usage.save('claude', state['fingerprint'], [{'id': 'five_hour', 'used_percent': 41, 'resets_at': time.time() + 1000}], 'test')
        revision = self.providers.accounts.revision('claude')
        marks = self.providers.accounts.marks('claude')
        for i in range(4):
            # Claude Code rewrites .credentials.json on every OAuth refresh, even mid-inference.
            self.claude_files(token='token-' + str(i + 2), expires=(time.time() + 7200 + i) * 1000)
            os.utime(self.profile / '.credentials.json', (time.time() + i + 1, time.time() + i + 1))
            self.assertEqual(self.providers.accounts.revision('claude'), revision)
            public = self.providers.usage.public('claude', pending=True)
            self.assertEqual(public['windows'][0]['used_percent'], 41)
            self.assertTrue(public['updating'])
        self.assertNotEqual(self.providers.accounts.marks('claude'), marks)  # the hint changes, the identity does not
        self.providers.cli['claude'] = 'claude-test.exe'
        with patch.object(self.providers, '_probe_status'):
            row = next(p for p in self.providers.status() if p['id'] == 'claude')
        self.assertTrue(row['connected'])
        self.assertEqual(row['account_label'], 'me@example.test')
        self.assertEqual(row['usage']['state'], 'observed')
        self.assertEqual(row['usage']['windows'][0]['used_percent'], 41)

    def test_different_account_clears_usage(self):
        state = self.claude_state()
        self.providers.usage.save('claude', state['fingerprint'], [{'id': 'five_hour', 'used_percent': 41}], 'test')
        self.claude_files(uuid='uuid-b', email='other@example.test')
        self.assertEqual(self.providers.usage.public('claude', pending=True)['windows'], [])
        with patch.object(self.providers, 'read_account', return_value=self.claude_account('other@example.test')):
            second = self.providers.accounts.check('claude', force=True)
        self.assertNotEqual(second['fingerprint'], state['fingerprint'])
        public = self.providers.usage.public('claude')
        self.assertEqual(public['windows'], [])
        self.assertEqual(public['state'], 'unavailable')

    def refresh(self, state, **options):
        opener = Mock()
        if 'error' in options:
            opener.open.side_effect = options['error']
        else:
            opener.open.return_value = io.StringIO(json.dumps(options['payload']))
        with patch('agentflow.usage.urllib.request.build_opener', return_value=opener):
            return self.providers.usage.refresh_claude(state, force=True), opener

    def test_401_keeps_data_and_never_raises(self):
        state = self.claude_state()
        self.providers.usage.save('claude', state['fingerprint'], [{'id': 'five_hour', 'used_percent': 41, 'resets_at': time.time() + 1000}], 'test')
        error = urllib.error.HTTPError('https://api.anthropic.com/api/oauth/usage', 401, 'Unauthorized', {}, None)
        updated, _ = self.refresh(state, error=error)
        self.assertFalse(updated)
        public = self.providers.usage.public('claude')
        self.assertEqual(public['windows'][0]['used_percent'], 41)
        self.assertEqual(public['notice'], CLAUDE_TOKEN_NOTICE)
        self.assertTrue(public['stale'])
        # An already expired access token is not even sent to the endpoint.
        self.claude_files(expires=(time.time() - 60) * 1000)
        updated, opener = self.refresh(state, payload={})
        self.assertFalse(updated)
        opener.open.assert_not_called()
        self.assertEqual(self.providers.usage.public('claude')['windows'][0]['used_percent'], 41)
        # 429 backs off and keeps data too.
        self.claude_files()
        updated, _ = self.refresh(state, error=urllib.error.HTTPError('u', 429, 'Too Many', {}, None))
        self.assertFalse(updated)
        self.assertEqual(self.providers.usage.public('claude')['windows'][0]['used_percent'], 41)
        self.assertGreater(self.providers.usage.backoff['claude'], time.monotonic())

    def test_every_window_is_parsed_with_its_duration(self):
        state = self.claude_state()
        reset = '2099-10-19T12:00:00.123456+00:00'
        updated, _ = self.refresh(state, payload={
            'five_hour': {'utilization': 23.5, 'resets_at': reset}, 'seven_day': {'utilization': 40.0, 'resets_at': reset},
            'seven_day_opus': {'utilization': 5.0, 'resets_at': None}, 'seven_day_sonnet': {'utilization': 7.25, 'resets_at': reset},
            'seven_day_oauth_apps': None, 'extra_usage': {'is_enabled': True, 'monthly_limit': 100, 'utilization': 12.5}})
        self.assertTrue(updated)
        windows = {w['id']: w for w in self.providers.usage.public('claude')['windows']}
        self.assertEqual(set(windows), {'five_hour', 'seven_day', 'seven_day_opus', 'seven_day_sonnet', 'extra_usage'})
        self.assertEqual([windows[k]['window_minutes'] for k in ('five_hour', 'seven_day', 'seven_day_opus', 'seven_day_sonnet')], [300, 10080, 10080, 10080])
        self.assertIsNone(windows['extra_usage']['window_minutes'])
        self.assertEqual(windows['seven_day_sonnet']['used_percent'], 7.25)
        self.assertIsNone(windows['seven_day_opus']['resets_at'])
        self.assertGreater(windows['five_hour']['resets_at'], time.time())

    def test_endpoint_percent_and_event_fraction_agree(self):
        state = self.claude_state()
        self.refresh(state, payload={'five_hour': {'utilization': 75.0, 'resets_at': None}})
        from_endpoint = self.providers.usage.public('claude')['windows'][0]['used_percent']
        self.providers.usage.claude_events([{'type': 'rate_limit_event', 'rate_limit_info': {'rateLimitType': 'five_hour', 'utilization': 0.75, 'resetsAt': time.time() + 500}}], state)
        from_event = {w['id']: w for w in self.providers.usage.public('claude')['windows']}['five_hour']['used_percent']
        self.assertEqual(from_endpoint, 75)
        self.assertEqual(from_event, 75)

    def test_events_tolerate_millisecond_resets_and_request_a_refresh(self):
        state = self.claude_state()
        reset_ms = (time.time() + 600) * 1000
        with patch.object(self.providers, 'kick_usage') as kick:
            self.providers.usage.claude_events([{'type': 'rate_limit_event', 'rate_limit_info': {'rateLimitType': 'five_hour', 'utilization': 0.1, 'resetsAt': reset_ms}}], state)
        self.assertLess(self.providers.usage.public('claude')['windows'][0]['resets_at'], time.time() + 700)
        kick.assert_called_once_with('claude')


class UsageStateTests(UsageBase):
    def codex_state(self, plan='plus'):
        (self.profile / 'auth.json').write_text(json.dumps({'tokens': {'account_id': 'workspace-a', 'access_token': 'one'}}))
        with patch.object(self.providers, 'read_account', return_value={'type': 'chatgpt', 'email': 'a@example.test', 'planType': plan}):
            return self.providers.accounts.check('codex', force=True)

    def test_expired_window_reads_zero_and_is_flagged_until_refreshed(self):
        state = self.codex_state()
        self.providers.usage.save('codex', state['fingerprint'], [
            {'id': 'codex:primary', 'used_percent': 88, 'resets_at': time.time() - 5, 'window_minutes': 300, 'bucket': 'codex'},
            {'id': 'codex:secondary', 'used_percent': 12, 'resets_at': time.time() + 5000, 'window_minutes': 10080, 'bucket': 'codex'}], 'test')
        for item in self.providers.usage.cache['codex']['windows']:
            item['observed_epoch'] = time.time() - 600  # observed before the first window rolled over
        public = self.providers.usage.public('codex')
        first, second = public['windows']
        self.assertTrue(first['expired'])
        self.assertEqual(first['used_percent'], 0)
        self.assertEqual(first['previous_used_percent'], 88)
        self.assertFalse(second['expired'])
        self.assertEqual(second['used_percent'], 12)
        self.assertTrue(self.providers.usage.has_expired('codex'))
        self.providers.usage.save('codex', state['fingerprint'], [{'id': 'codex:primary', 'used_percent': 1, 'resets_at': time.time() + 9000, 'bucket': 'codex'}], 'test', merge=True)
        self.assertFalse(self.providers.usage.has_expired('codex'))

    def test_plan_and_token_changes_do_not_drop_a_codex_observation(self):
        state = self.codex_state('plus')
        self.assertEqual(identity_digest({'type': 'chatgpt', 'email': 'a@example.test', 'planType': 'plus', 'tokenExpiresAt': 1}),
                         identity_digest({'type': 'chatgpt', 'email': 'a@example.test', 'planType': 'pro', 'tokenExpiresAt': 2}))
        self.assertNotIn('planType', stable_identity({'planType': 'x', 'email': 'e'}))
        result = {'account': {'type': 'chatgpt', 'email': 'a@example.test', 'planType': 'pro', 'accessTokenExpiresAt': 99},
                  'limits': {'rateLimitsByLimitId': {'codex': {'primary': {'used_percent': 33, 'window_duration_mins': 300, 'resets_at': (time.time() + 600) * 1000}}}}}
        self.assertTrue(self.providers.usage.apply_codex(state, result))
        window = self.providers.usage.public('codex')['windows'][0]
        self.assertEqual(window['used_percent'], 33)
        self.assertEqual(window['window_minutes'], 300)
        self.assertLess(window['resets_at'], time.time() + 700)  # milliseconds were normalised to seconds
        self.assertEqual(self.providers.usage.public('codex')['plan_type'], 'plus')

    def test_state_saved_before_the_identity_digest_change_keeps_its_fingerprint(self):
        state = self.codex_state()
        legacy = {k: v for k, v in state.items() if k != 'plan_type'}
        legacy['fingerprint'] = 'legacy-fingerprint'
        self.providers.accounts.current['codex'] = legacy
        self.providers.accounts.persisted['codex'] = legacy
        with patch.object(self.providers, 'read_account', return_value={'type': 'chatgpt', 'email': 'a@example.test', 'planType': 'plus'}):
            again = self.providers.accounts.check('codex', force=True)
        self.assertEqual(again['fingerprint'], 'legacy-fingerprint')
        self.assertNotIn('Current account detected', ' '.join(e['title'] for e in self.store.all('events')[-1:]))

    def test_real_identity_change_during_codex_read_discards_the_update(self):
        state = self.codex_state()
        (self.profile / 'auth.json').write_text(json.dumps({'tokens': {'account_id': 'workspace-a'}}))
        result = {'account': {'type': 'chatgpt', 'email': 'other@example.test'}, 'limits': {'rateLimits': {'primary': {'usedPercent': 5}}}}
        with patch.object(self.providers, 'refresh_models', return_value=[]):
            with self.assertRaises(AccountChanged):
                self.providers.usage.apply_codex(state, result)
        self.assertEqual(self.providers.usage.public('codex')['windows'], [])

    def test_legacy_rate_limits_only_and_missing_limits_are_handled(self):
        state = self.codex_state()
        legacy = {'limits': {'rateLimits': {'limitId': 'codex', 'planType': 'pro', 'primary': {'usedPercent': 10, 'windowDurationMins': 300, 'resetsAt': time.time() + 100}}}}
        self.assertTrue(self.providers.usage.apply_codex(state, legacy))
        self.assertEqual(self.providers.usage.public('codex')['plan_type'], 'pro')
        # A failed limits call keeps the observation and explains why.
        self.assertFalse(self.providers.usage.apply_codex(state, {'account': {'type': 'chatgpt', 'email': 'a@example.test'}, 'limits': {}, 'limits_error': 'boom'}))
        public = self.providers.usage.public('codex')
        self.assertEqual(public['windows'][0]['used_percent'], 10)
        self.assertIn('last confirmed', public['notice'])

    def test_failed_limits_read_does_not_look_like_an_account_failure(self):
        process = Mock(pid=None)
        process.poll.return_value = None
        lines = [json.dumps({'id': 1, 'result': {}}), json.dumps({'id': 2, 'result': {'account': {'type': 'chatgpt'}}}),
                 json.dumps({'id': 3, 'error': {'message': 'nope'}})]
        class Pipe:
            def __iter__(self):
                return iter([line + '\n' for line in lines])
            def close(self):
                pass
        process.stdout = Pipe()
        self.providers.cli['codex'] = 'metadata-test.exe'
        with patch('agentflow.providers.subprocess.Popen', return_value=process):
            result = self.providers._codex_models(include_models=False, read_usage=True)
        self.assertEqual(result['account'], {'type': 'chatgpt'})
        self.assertEqual(result['limits'], {})
        self.assertIn('limits_error', result)

    def test_confirmed_usage_is_available_immediately_after_restart_for_the_same_identity(self):
        state = self.claude_state()
        self.providers.usage.save('claude', state['fingerprint'], [{'id': 'five_hour', 'used_percent': 41, 'resets_at': time.time() + 900}], 'test')
        with patch.object(Accounts, 'home', return_value=self.profile):
            reopened = Providers(self.store, self.vault, self.engine.workspace)
            reopened.cli['claude'] = 'claude-test.exe'
            with patch.object(reopened, '_schedule'):
                row = next(p for p in reopened.status() if p['id'] == 'claude')
            self.assertEqual(row['usage']['windows'][0]['used_percent'], 41)
            self.assertTrue(row['connected'])
            self.claude_files(uuid='uuid-z')
            switched = Providers(self.store, self.vault, self.engine.workspace)
            self.assertNotIn('claude', switched.accounts.current)
            self.assertEqual(switched.usage.public('claude')['windows'], [])


class CadenceTests(UsageBase):
    def plan(self, provider, attempt, clock, polled=True, revision='r', marks='m'):
        base = {'identity_at': 0, 'usage_at': 0, 'revision': 'r', 'marks': 'm'}
        return self.providers._probe_plan(provider, {**base, **attempt}, revision, marks, clock, polled)

    def test_first_probe_checks_identity_and_usage(self):
        plan, _ = self.providers._probe_plan('codex', {}, 'r', 'm', 10, True)
        self.assertTrue(plan['identity'] and plan['usage'])

    def test_nothing_is_due_right_after_a_probe(self):
        self.assertEqual(self.plan('codex', {}, 30), (None, None))
        self.assertEqual(self.plan('claude', {}, 100), (None, None))

    def test_usage_cadence_per_provider_and_viewer_state(self):
        plan, _ = self.plan('codex', {}, 61)
        self.assertEqual((plan['identity'], plan['usage']), (False, True))
        self.assertEqual(self.plan('codex', {}, 250, polled=False), (None, None))  # idle: five minutes
        plan, _ = self.plan('codex', {}, 301, polled=False)
        self.assertTrue(plan['usage'])
        plan, _ = self.plan('claude', {'identity_at': 500}, 601)   # the rate-limited Claude endpoint: ten minutes while watched
        self.assertEqual((plan['identity'], plan['usage']), (False, True))
        self.assertEqual(self.plan('claude', {'identity_at': 500}, 599), (None, None))
        self.assertEqual(self.plan('claude', {'identity_at': 1000}, 1100, polled=False), (None, None))   # idle: twenty minutes
        plan, _ = self.plan('claude', {'identity_at': 1200}, 1201, polled=False)
        self.assertTrue(plan['usage'])

    def test_identity_is_rechecked_at_most_every_five_minutes(self):
        plan, _ = self.plan('claude', {'usage_at': 299}, 299)
        self.assertIsNone(plan)
        plan, _ = self.plan('claude', {'usage_at': 301}, 301)
        self.assertTrue(plan['identity'])

    def test_revision_change_checks_both_immediately(self):
        plan, _ = self.plan('codex', {}, 5, revision='other')
        self.assertTrue(plan['identity'] and plan['usage'])

    def test_credential_file_change_only_triggers_a_debounced_background_recheck(self):
        self.assertEqual(self.plan('claude', {}, 10, marks='changed'), (None, None))
        plan, _ = self.plan('claude', {}, 25, marks='changed')
        self.assertTrue(plan['identity'] and plan['usage'])

    def test_expired_window_requests_a_refresh_after_debounce(self):
        with patch.object(self.providers.usage, 'has_expired', return_value=True):
            self.assertEqual(self.plan('codex', {}, 10), (None, None))
            plan, _ = self.plan('codex', {}, 25)
        self.assertTrue(plan['usage'])

    def test_inference_triggers_a_debounced_refresh(self):
        clock = time.monotonic()
        attempt = {'identity_at': clock - 10, 'usage_at': clock - 5, 'revision': 'r', 'marks': 'm'}
        self.providers.usage.pending_inference['codex'] = clock
        plan, delay = self.providers._probe_plan('codex', attempt, 'r', 'm', clock, True)
        self.assertIsNone(plan)
        self.assertAlmostEqual(delay, 15, delta=0.5)
        attempt['usage_at'] = clock - 25
        plan, delay = self.providers._probe_plan('codex', attempt, 'r', 'm', clock, True)
        self.assertTrue(plan['usage'])
        # After a refresh that started later than the inference, nothing is pending.
        attempt['usage_at'] = clock + 1
        self.assertEqual(self.providers._probe_plan('codex', attempt, 'r', 'm', clock + 30, True), (None, None))

    def test_schedule_arms_one_timer_and_never_runs_a_second_probe_while_one_is_slow(self):
        self.providers.cli['codex'] = 'codex-test.exe'
        release = threading.Event()
        calls = []
        def slow(provider, plan=None):
            calls.append(plan)
            release.wait(timeout=5)
        with patch.object(self.providers, '_probe_status', side_effect=slow):
            try:
                started = time.monotonic()
                self.providers.status()
                self.providers.status()
                self.providers.status()
                self.assertLess(time.monotonic() - started, 0.5)
                time.sleep(0.1)
                self.assertEqual(len(calls), 1)
            finally:
                release.set()
                for thread in self.providers.status_threads.values():
                    thread.join(timeout=5)

    def test_inference_debounce_arms_a_timer_instead_of_probing(self):
        self.providers.cli['claude'] = 'claude-test.exe'
        clock = time.monotonic()
        self.providers.status_attempts['claude'] = {'identity_at': clock - 5, 'usage_at': clock - 5, 'revision': self.providers.accounts.revision('claude'), 'marks': self.providers.accounts.marks('claude')}
        self.providers.usage.pending_inference['claude'] = clock
        with patch.object(self.providers, '_arm_timer') as arm, patch.object(self.providers, '_probe_status') as probe:
            self.providers._schedule('claude', polled=True)
        probe.assert_not_called()
        self.assertAlmostEqual(arm.call_args.args[1], 120, delta=1)   # Claude: one refresh two minutes after the inference

    def test_finished_inference_is_detected_without_hooking_infer(self):
        with patch.object(self.providers.usage, 'note_inference') as note:
            self.providers.active_accounts['run-1'] = {'provider': 'codex', 'fingerprint': 'f'}
            self.providers._watch_inference()
            note.assert_not_called()
            self.providers.active_accounts.pop('run-1')
            self.providers._watch_inference()
            note.assert_called_once_with('codex')
            note.reset_mock()
            self.providers.account_runs['run-2'] = {'provider': 'claude', 'fingerprint': 'f'}
            self.providers._watch_inference()
            note.assert_called_once_with('claude')

    def test_background_tick_only_probes_when_nobody_is_polling(self):
        self.providers.cli['codex'] = 'codex-test.exe'
        with patch.object(self.providers, '_schedule') as schedule:
            self.providers.last_poll = time.monotonic()
            self.providers.background_tick()
            schedule.assert_not_called()
            self.providers.last_poll = time.monotonic() - 120
            self.providers.background_tick()
            self.assertEqual({c.args[0] for c in schedule.call_args_list}, {'codex', 'claude', 'gemini'})
            self.assertTrue(all(c.kwargs['polled'] is False for c in schedule.call_args_list))

    def test_codex_probe_reads_account_and_limits_with_one_spawn(self):
        self.providers.cli['codex'] = 'codex-test.exe'
        (self.profile / 'auth.json').write_text(json.dumps({'tokens': {'account_id': 'workspace-a'}}))
        payload = {'account': {'type': 'chatgpt', 'email': 'a@example.test', 'planType': 'plus'},
                   'limits': {'rateLimits': {'primary': {'usedPercent': 21, 'windowDurationMins': 300, 'resetsAt': time.time() + 500}}}}
        self.providers.account_patch = None
        with patch.object(self.providers, 'read_account', wraps=Providers.read_account.__get__(self.providers)), \
             patch.object(self.providers, '_codex_models', return_value=payload) as spawn:
            self.providers._probe_status('codex')
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(self.providers.usage.public('codex')['windows'][0]['used_percent'], 21)
        self.assertEqual(self.providers.accounts.current['codex']['label'], 'a@example.test')

    def test_usage_refresh_failure_keeps_windows_and_account(self):
        state = self.claude_state()
        self.providers.usage.save('claude', state['fingerprint'], [{'id': 'five_hour', 'used_percent': 41}], 'test')
        self.providers.cli['claude'] = 'claude-test.exe'
        with patch.object(self.providers.usage, 'refresh_claude', side_effect=DomainError('Temporary failure', 503)), \
             patch.object(self.providers, 'read_account', return_value=self.claude_account()):
            self.providers._probe_status('claude', {'identity': False, 'usage': True})
        public = self.providers.usage.public('claude')
        self.assertEqual(public['windows'][0]['used_percent'], 41)
        self.assertTrue(public['stale'])
        self.assertEqual(self.providers.accounts.current['claude']['label'], 'me@example.test')


class HousekeepingBase(unittest.TestCase):
    def setUp(self):
        runtime.RuntimeTests.setUp(self)
        self.hk = self.engine.housekeeping
        self.directory = self.store.directory
        self.media = self.engine.workspace / 'media'

    def tearDown(self):
        thread = self.hk.thread
        if thread:
            thread.join(timeout=5)
        runtime.RuntimeTests.tearDown(self)

    def age(self, path, days):
        stamp = time.time() - days * DAY
        os.utime(path, (stamp, stamp))

    def write(self, path, size=100, days=0):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x' * size)
        if days:
            self.age(path, days)
        return path

    def iso(self, days):
        return (datetime.now().astimezone() - timedelta(days=days)).isoformat()

    def backdate(self, kind, record_id, days, fields=('created_at', 'updated_at', 'completed_at')):
        item = self.store.get(kind, record_id)
        item.pop('version')
        for field in fields:
            if field in item or field != 'completed_at':
                item[field] = self.iso(days)
        self.store.conn.execute('UPDATE records SET payload=? WHERE kind=? AND id=?', (json.dumps(item), kind, record_id))

    def make_run(self, status='done', days=30, trace=True):
        run = self.store.create('runs', {'agent_id': 'a-lead', 'status': status, 'result': 'final answer', 'summary': 'kept',
            'trace': [{'role': 'assistant', 'content': 'x' * 6000}] if trace else [], 'pending_calls': [{'name': 'a'}],
            'operation_fingerprints': ['a', 'b'], 'prompt': 'p', 'completed_at': self.iso(days)})
        self.backdate('runs', run['id'], days)
        return self.store.get('runs', run['id'])


class AutoCleanupTests(HousekeepingBase):
    def test_registered_on_engine_with_handler(self):
        self.assertIsInstance(self.engine.housekeeping, hk.Housekeeping)
        self.assertEqual(self.engine.decision_handlers['cleanup'], self.hk.decision)

    def test_stale_tmp_open_files_and_surplus_prompts_only(self):
        stale = self.write(self.directory / 'old.tmp', days=1)
        fresh = self.write(self.directory / 'new.tmp')
        keep = self.write(self.directory / 'workspace.sqlite3.keep', days=40)
        old_copy = self.write(self.directory / 'open-files' / 'open-1-a.pdf', days=2)
        new_copy = self.write(self.directory / 'open-files' / 'open-2-b.pdf')
        prompts = [self.write(self.directory / 'prompts' / ('p%02d.txt' % i), 10, days=0.5 - i * 0.001) for i in range(60)]
        results = self.hk.run_auto()
        self.assertEqual(set(results), set(hk.AUTO))
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists() and keep.exists())
        self.assertFalse(old_copy.exists())
        self.assertTrue(new_copy.exists())
        survivors = [p for p in prompts if p.exists()]
        self.assertEqual(len(survivors), 50)
        self.assertEqual({p.name for p in survivors}, {p.name for p in prompts[10:]})  # newest kept
        self.assertEqual(results['temp']['files'], 1)
        self.assertEqual(results['prompts']['files'], 10)

    def test_prompts_are_spared_while_a_run_is_active_unless_a_day_old(self):
        prompts = [self.write(self.directory / 'prompts' / ('p%02d.txt' % i), 10, days=0.5 - i * 0.001) for i in range(55)]
        old = self.write(self.directory / 'prompts' / 'ancient.txt', 10, days=3)
        self.engine.active.add('a-lead')
        try:
            self.hk.clean('prompts')
        finally:
            self.engine.active.clear()
        self.assertTrue(all(p.exists() for p in prompts))  # recent files may still be read by a live run
        self.assertFalse(old.exists())

    def test_native_sessions_are_pruned_keeping_live_runs(self):
        running = self.make_run(status='running', days=0)
        self.make_run(status='done', days=1)
        sessions = Mock()
        sessions.prune.return_value = {'freed_bytes': 2048, 'files': 2}
        self.providers._sessions = sessions
        result = self.hk.clean('native_sessions')
        self.assertEqual(result, {'freed_bytes': 2048, 'files': 2})
        self.assertIn(running['id'], sessions.prune.call_args.kwargs['keep_run_ids'])
        self.assertEqual(sessions.prune.call_args.kwargs['older_than_s'], 3600)
        sessions.prune.side_effect = OSError('disk')
        self.assertEqual(self.hk.clean('native_sessions'), {'freed_bytes': 0, 'files': 0})  # never raises into the heartbeat
        with patch.object(self.hk, '_sessions', return_value=None):
            self.assertEqual(self.hk.clean('native_sessions'), {'freed_bytes': 0, 'files': 0})

    def test_history_compaction_trims_old_finished_runs_and_keeps_results(self):
        old = self.make_run(days=30)
        recent = self.make_run(days=1)
        active = self.make_run(status='running', days=30)
        event = self.store.create('tool_events', {'run_id': old['id'], 'name': 'web.fetch', 'status': 'done', 'result': {'text': 'y' * 20000}})
        self.backdate('tool_events', event['id'], 30, fields=('created_at', 'updated_at'))
        live_event = self.store.create('tool_events', {'run_id': active['id'], 'name': 'web.fetch', 'status': 'done', 'result': {'text': 'z' * 20000}})
        self.backdate('tool_events', live_event['id'], 30, fields=('created_at', 'updated_at'))
        result = self.hk._clean_database()
        trimmed = self.store.get('runs', old['id'])
        self.assertEqual((trimmed['trace'], trimmed['pending_calls'], trimmed['operation_fingerprints']), ([], [], []))
        self.assertEqual((trimmed['result'], trimmed['summary']), ('final answer', 'kept'))
        self.assertEqual(trimmed['version'], old['version'])  # no optimistic-lock churn for the UI
        self.assertEqual(self.store.get('runs', recent['id'])['trace'], recent['trace'])
        self.assertEqual(self.store.get('runs', active['id'])['trace'], active['trace'])
        shrunk = self.store.get('tool_events', event['id'])
        self.assertLessEqual(len(json.dumps(shrunk['result'])), 2048)
        self.assertTrue(shrunk['result']['truncated'])
        self.assertEqual(self.store.get('tool_events', live_event['id'])['result']['text'], 'z' * 20000)
        self.assertGreaterEqual(result['files'], 2)
        self.assertEqual(self.hk._clean_database()['files'], 0)  # idempotent

    def test_retention_setting_controls_history_age(self):
        run = self.make_run(days=3)
        self.hk._clean_database()
        self.assertTrue(self.store.get('runs', run['id'])['trace'])
        self.store.settings({'housekeeping': {'retention_days': 2}})
        self.hk._clean_database()
        self.assertEqual(self.store.get('runs', run['id'])['trace'], [])

    def test_deliverables_are_never_touched(self):
        report = self.write(self.engine.workspace / 'reports' / 'q3.md', 500, days=400)
        old_pdf = self.write(self.engine.workspace / 'report.pdf', 500, days=400)
        artifact = self.store.create('artifacts', {'path': 'report.pdf', 'kind': 'file', 'title': 'Report'})
        shot = self.write(self.media / 'screenshot-aaaaaaaaaaaa.png', 100, days=40)
        self.store.create('artifacts', {'path': 'media/screenshot-aaaaaaaaaaaa.png', 'kind': 'file', 'title': 'Deliverable'})
        self.hk.run_auto()
        self.hk.cleanup(list(hk.ASK))
        self.assertTrue(report.exists() and old_pdf.exists())
        self.assertTrue(shot.exists())  # re-registered as a file deliverable, so not screenshot evidence
        self.assertNotIn('file_removed', self.store.get('artifacts', artifact['id']))

    def test_tick_is_cheap_delayed_and_runs_in_the_background(self):
        with patch.object(self.hk, 'run_scheduled') as scheduled:
            self.hk.tick()
            self.assertIsNone(self.hk.thread)  # startup grace period
            self.hk.startup_delay = 0
            self.hk.tick()
            self.hk.thread.join(timeout=2)
            scheduled.assert_called_once()
            self.hk.thread = None
            self.hk.state['last_run'] = time.time()
            self.hk.tick()
            self.assertIsNone(self.hk.thread)  # not due for six hours

    def test_engine_tick_calls_the_hook(self):
        with patch.object(self.hk, 'tick') as tick:
            self.engine.tick()
        tick.assert_called_once()

    def test_scheduled_run_honours_auto_cleanup_switch(self):
        stale = self.write(self.directory / 'old.tmp', days=1)
        self.store.settings({'housekeeping': {'auto_cleanup': False}})
        self.hk.run_scheduled()
        self.assertTrue(stale.exists())
        self.store.settings({'housekeeping': {'auto_cleanup': True}})
        self.hk.run_scheduled()
        self.assertFalse(stale.exists())
        self.assertEqual(self.hk.state['last_cleanup']['files'], 1)

    def test_settings_validation_and_merge(self):
        result = self.service.settings({'housekeeping': {'min_free_gb': 10}})
        self.assertEqual(result['housekeeping'], {'auto_cleanup': True, 'min_free_gb': 10, 'retention_days': 7})
        result = self.service.settings({'housekeeping': {'retention_days': 30}})
        self.assertEqual(result['housekeeping'], {'auto_cleanup': True, 'min_free_gb': 10, 'retention_days': 30})
        for bad in ({'min_free_gb': 0}, {'min_free_gb': 501}, {'retention_days': 0}, {'retention_days': 366},
                    {'auto_cleanup': 'yes'}, {'retention_days': True}, 'text'):
            with self.assertRaises(DomainError):
                self.service.settings({'housekeeping': bad})


class AskFirstTests(HousekeepingBase):
    def setUp(self):
        super().setUp()
        patcher = patch('agentflow.housekeeping.MIN_ASK_BYTES', 1)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.old_name = 'reset-' + (datetime.now() - timedelta(days=30)).strftime('%Y%m%d-%H%M%S') + '-000'
        self.new_name = 'reset-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-001'
        self.artifacts = self.root / 'artifacts'
        self.old_archive = self.artifacts / self.old_name
        self.write(self.old_archive / 'runtime' / 'workspace.sqlite3', 4000)
        self.write(self.old_archive / 'company' / 'doc.md', 100)
        self.new_archive = self.artifacts / self.new_name
        self.write(self.new_archive / 'runtime' / 'workspace.sqlite3', 4000)
        self.other = self.write(self.artifacts / 'not-a-reset' / 'keep.txt', 100, days=90)

    def low_disk(self, free_gb=1):
        return patch('agentflow.housekeeping.shutil.disk_usage', return_value=Usage(500 * hk.GB, 499 * hk.GB, free_gb * hk.GB))

    def scenario(self):
        self.shot_old = self.write(self.media / 'screenshot-aaaaaaaaaaaa.png', 300, days=20)
        self.shot_used = self.write(self.media / 'screenshot-bbbbbbbbbbbb.png', 300, days=20)
        self.shot_new = self.write(self.media / 'screenshot-cccccccccccc.png', 300)
        self.shot_artifact = self.store.create('artifacts', {'path': 'media/screenshot-aaaaaaaaaaaa.png', 'kind': 'screenshot', 'title': 'Evidence'})
        self.store.create('messages', {'agent_id': 'a-lead', 'role': 'assistant', 'content': 'See ![bug](media/screenshot-bbbbbbbbbbbb.png)', 'channel': 'dm-a-lead'})
        finished = self.store.create('projects', {'title': 'Done', 'status': 'done', 'channel': 'c1'})
        active = self.store.create('projects', {'title': 'Live', 'status': 'active', 'channel': 'c2'})
        att = self.directory / 'attachments'
        self.write(att / 'upload-old.txt', 700)
        self.write(att / 'upload-live.txt', 700)
        self.write(att / 'upload-orphan.txt', 700)
        self.store.create('attachments', {'id': 'upload-old', 'name': 'a.txt', 'suffix': '.txt', 'project_id': finished['id'], 'created_at': self.iso(60)})
        self.store.create('attachments', {'id': 'upload-live', 'name': 'b.txt', 'suffix': '.txt', 'project_id': active['id'], 'created_at': self.iso(60)})
        self.store.create('attachments', {'id': 'upload-orphan', 'name': 'c.txt', 'suffix': '.txt', 'project_id': None, 'created_at': self.iso(60)})

    def test_nothing_is_asked_when_space_is_fine(self):
        self.scenario()
        with patch('agentflow.housekeeping.shutil.disk_usage', return_value=Usage(500 * hk.GB, 100 * hk.GB, 400 * hk.GB)):
            self.assertIsNone(self.hk.check_space())
        self.assertEqual([q for q in self.store.all('decisions')], [])

    def test_low_space_posts_one_non_blocking_decision_listing_categories(self):
        self.scenario()
        with self.low_disk():
            result = self.hk.check_space()
            self.assertIsNone(self.hk.check_space())  # one at a time
        question = self.store.get('decisions', result['decision_id'])
        self.assertFalse(question['blocking'])
        self.assertEqual(question['action_handler'], 'cleanup')
        self.assertEqual(question['action_field'], 'cleanup_action')
        self.assertEqual([a['value'] for a in question['actions']], ['clean', 'skip'])
        self.assertTrue(question['actions'][0]['label'].startswith('Free '))
        self.assertEqual(question['actions'][1]['label'], 'Not now')
        for text in ('Reset backups', 'Browser screenshots', 'Old attachments'):
            self.assertIn(text, question['context'])
        self.assertIn('Only 1.0 GB is free', question['question'])
        self.assertEqual(question['channel'], 'dm-a-lead')
        self.assertEqual(len([q for q in self.store.all('decisions')]), 1)

    def test_large_data_alone_triggers_the_question(self):
        self.scenario()
        with patch('agentflow.housekeeping.shutil.disk_usage', return_value=Usage(500 * hk.GB, 100 * hk.GB, 400 * hk.GB)), \
             patch.object(self.hk, 'data_bytes', return_value=3 * hk.GB):
            result = self.hk.check_space()
        self.assertIn('has grown', self.store.get('decisions', result['decision_id'])['question'])

    def test_candidates_follow_the_rules(self):
        self.scenario()
        groups = self.hk.ask_groups()
        names = lambda c: {Path(i['path']).name for i in groups.get(c, [])}
        self.assertEqual(names('reset_archives'), {self.old_name})  # newer and non-matching folders stay
        self.assertEqual(names('screenshots'), {'screenshot-aaaaaaaaaaaa.png'})  # referenced and recent ones stay
        self.assertEqual(names('attachments'), {'upload-old.txt', 'upload-orphan.txt'})  # active project stays
        self.assertNotIn('browser_cache', groups)  # no browser directories exist

    def test_clean_answer_performs_exactly_the_listed_deletions(self):
        self.scenario()
        with self.low_disk():
            result = self.hk.check_space()
        listed = {Path(i['path']).name for i in self.hk.state['pending']['items']}
        self.assertEqual(listed, {self.old_name, 'screenshot-aaaaaaaaaaaa.png', 'upload-old.txt', 'upload-orphan.txt'})
        # Something new appears after the question was posted: it is not on the list, so it stays.
        self.write(self.artifacts / ('reset-' + (datetime.now() - timedelta(days=40)).strftime('%Y%m%d-%H%M%S') + '-005') / 'a.bin', 100)
        late = next(self.artifacts.glob('reset-*-005'))
        self.engine.team.answer(result['decision_id'], {'answer': 'Free now', 'cleanup_action': 'clean'})
        deadline = time.time() + 5
        while 'last_cleanup' not in self.hk.state and time.time() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.old_archive.exists())
        self.assertTrue(self.new_archive.exists() and self.other.exists() and late.exists())
        self.assertFalse(self.shot_old.exists())
        self.assertTrue(self.shot_used.exists() and self.shot_new.exists())
        self.assertTrue(self.store.get('artifacts', self.shot_artifact['id'])['file_removed'])
        self.assertFalse((self.directory / 'attachments' / 'upload-old.txt').exists())
        self.assertFalse((self.directory / 'attachments' / 'upload-orphan.txt').exists())
        self.assertTrue((self.directory / 'attachments' / 'upload-live.txt').exists())
        self.assertTrue(self.store.get('attachments', 'upload-old')['file_removed'])
        self.assertGreater(self.hk.state['last_cleanup']['freed_bytes'], 4000)
        self.assertEqual(self.store.get('decisions', result['decision_id'])['status'], 'answered')
        self.assertTrue(any('Disk cleanup finished' in m['content'] for m in self.store.all('messages')))
        self.assertFalse(any(r.get('kind') == 'decision_followup' for r in self.store.all('runs')))  # no model turn is spent

    def test_not_now_snoozes_for_24_hours(self):
        self.scenario()
        with self.low_disk():
            result = self.hk.check_space()
            self.engine.team.answer(result['decision_id'], {'answer': 'Not now', 'cleanup_action': 'skip'})
            self.assertGreater(self.hk.state['snoozed_until'], time.time() + 23 * 3600)
            self.assertIsNone(self.hk.check_space())
            self.assertTrue(self.old_archive.exists())
            self.hk.state['snoozed_until'] = time.time() - 1
            self.assertIsNotNone(self.hk.check_space())

    def test_invalid_action_value_is_rejected_by_the_decision_flow(self):
        self.scenario()
        with self.low_disk():
            result = self.hk.check_space()
        with self.assertRaises(DomainError):
            self.engine.team.answer(result['decision_id'], {'answer': 'x', 'cleanup_action': 'delete_everything'})
        self.assertTrue(self.old_archive.exists())

    def test_browser_and_parser_caches_only_when_unused_for_30_days(self):
        browsers = self.write(self.directory / 'browsers' / 'chromium' / 'chrome.bin', 5000)
        packages = self.write(self.directory / 'browser-packages' / 'playwright' / 'x.py', 100)
        parser = self.write(self.directory / 'parser-packages' / 'pypdf' / 'y.py', 100)
        self.assertEqual(self.hk.collect('browser_cache')[0], [])  # installed today
        for path in (browsers, packages, parser):
            for folder in [path.parent] + list(path.parents[:2]):
                if str(folder).startswith(str(self.directory)) and folder != self.directory:
                    self.age(folder, 60)
        self.assertEqual({Path(i['path']).name for i in self.hk.collect('browser_cache')[0]}, {'browsers', 'browser-packages'})
        self.assertEqual({Path(i['path']).name for i in self.hk.collect('parser_packages')[0]}, {'parser-packages'})
        run = self.make_run(days=1)
        self.store.create('tool_events', {'run_id': run['id'], 'name': 'browser.open', 'status': 'done', 'result': {}})
        self.assertEqual(self.hk.collect('browser_cache')[0], [])  # used recently
        self.store.create('attachments', {'id': 'upload-pdf', 'name': 'a.pdf', 'suffix': '.pdf', 'project_id': None})
        self.assertEqual(self.hk.collect('parser_packages')[0], [])

    def test_symlinks_and_paths_outside_roots_are_refused(self):
        outside = self.root / 'precious'
        self.write(outside / 'keep.txt', 100)
        target = self.artifacts / ('reset-' + (datetime.now() - timedelta(days=50)).strftime('%Y%m%d-%H%M%S') + '-002')
        target.mkdir()
        try:
            os.symlink(outside, target / 'link', target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks are unavailable')
        names = {Path(i['path']).name for i in self.hk.collect('reset_archives')[0]}
        self.assertNotIn(target.name, names)  # contains a link that may escape the root
        self.assertIsNone(self.hk.remove(target, [self.artifacts]))
        self.assertTrue((outside / 'keep.txt').exists())
        link = self.artifacts / ('reset-' + (datetime.now() - timedelta(days=51)).strftime('%Y%m%d-%H%M%S') + '-003')
        os.symlink(outside, link, target_is_directory=True)
        self.assertIsNone(self.hk.remove(link, [self.artifacts]))
        self.assertTrue((outside / 'keep.txt').exists())
        self.assertIsNone(self.hk.remove(outside / 'keep.txt', [self.artifacts]))
        self.assertIsNone(self.hk.remove(self.artifacts, [self.artifacts]))  # the root itself
        self.assertEqual(self.hk.clean_items([{'path': str(outside / 'keep.txt'), 'root': str(self.artifacts), 'record': None}]), {'freed_bytes': 0, 'files': 0})
        self.assertTrue((outside / 'keep.txt').exists())


class StorageApiTests(HousekeepingBase):
    def request(self, method, url, body=None):
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.service, 'session', 0))
        port = server.server_port
        server.RequestHandlerClass = make_handler(self.service, 'session', port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection('127.0.0.1', port)
        try:
            headers = {'Cookie': 'af_session=session', 'Content-Type': 'application/json', 'Origin': 'http://127.0.0.1:%d' % port}
            conn.request(method, url, json.dumps(body) if body is not None else None, headers)
            res = conn.getresponse()
            return res.status, json.loads(res.read() or b'{}')
        finally:
            conn.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_get_storage_matches_the_contract(self):
        self.write(self.directory / 'open-files' / 'x.pdf', 1000, days=3)
        status, data = self.request('GET', '/api/storage')
        self.assertEqual(status, 200)
        for key in ('total_bytes', 'free_bytes', 'disk_total_bytes', 'categories', 'last_cleanup', 'settings'):
            self.assertIn(key, data)
        self.assertEqual(data['settings'], {'auto_cleanup': True, 'min_free_gb': 5, 'retention_days': 7})
        self.assertEqual([c['id'] for c in data['categories']], list(hk.AUTO + hk.ASK))
        for category in data['categories']:
            self.assertTrue({'id', 'label', 'description', 'bytes', 'files', 'auto'} <= set(category))
        by_id = {c['id']: c for c in data['categories']}
        self.assertTrue(by_id['temp']['auto'] and not by_id['screenshots']['auto'])
        self.assertEqual((by_id['open_files']['bytes'], by_id['open_files']['files']), (1000, 1))
        self.assertGreater(data['total_bytes'], 0)

    def test_post_cleanup_runs_requested_categories_only(self):
        old = self.write(self.directory / 'open-files' / 'x.pdf', 1000, days=3)
        stale = self.write(self.directory / 'stale.tmp', 10, days=1)
        status, data = self.request('POST', '/api/storage/cleanup', {'categories': ['open_files']})
        self.assertEqual(status, 200)
        self.assertEqual((data['freed_bytes'], data['files']), (1000, 1))
        self.assertEqual([c['id'] for c in data['categories']], ['open_files'])
        self.assertFalse(old.exists())
        self.assertTrue(stale.exists())
        status, data = self.request('POST', '/api/storage/cleanup', {'categories': ['nope']})
        self.assertEqual(status, 400)
        status, data = self.request('POST', '/api/storage/cleanup', {'categories': 'temp'})
        self.assertEqual(status, 400)
        status, data = self.request('GET', '/api/storage')
        self.assertEqual(data['last_cleanup']['freed_bytes'], 1000)

    def test_settings_endpoint_accepts_housekeeping(self):
        status, data = self.request('POST', '/api/settings', {'housekeeping': {'min_free_gb': 20, 'retention_days': 14}})
        self.assertEqual(status, 200)
        self.assertEqual(data['housekeeping'], {'auto_cleanup': True, 'min_free_gb': 20, 'retention_days': 14})
        status, data = self.request('POST', '/api/settings', {'housekeeping': {'min_free_gb': 0}})
        self.assertEqual(status, 400)


if __name__ == '__main__':
    unittest.main()
