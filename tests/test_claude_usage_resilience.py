"""Claude usage must not vanish: events without utilization keep known percentages, partial answers keep other windows,
a 429 is a quiet back-off that survives a restart, and the polling cadence is slow enough not to cause it."""
import io
import json
import time
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import Mock, patch

from test_usage_housekeeping import UsageBase
from agentflow.usage import Usage, parse_retry_after, BACKOFF_MIN, BACKOFF_MAX


def event(kind='five_hour', utilization=None, reset=None, status='allowed'):
    info = {'rateLimitType': kind, 'status': status, 'resetsAt': reset}
    if utilization is not None:
        info['utilization'] = utilization
    return {'type': 'rate_limit_event', 'rate_limit_info': info}


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


class ClaudeResilienceBase(UsageBase):
    def endpoint(self, state, payload=None, error=None):
        opener = Mock()
        if error:
            opener.open.side_effect = error
        else:
            opener.open.return_value = io.StringIO(json.dumps(payload))
        with patch('agentflow.usage.urllib.request.build_opener', return_value=opener):
            return self.providers.usage.refresh_claude(state, force=True)

    def windows(self):
        return {w['id']: w for w in self.providers.usage.public('claude')['windows']}

    def rate_limited(self, retry_after=None):
        return urllib.error.HTTPError('u', 429, 'Too Many', {'Retry-After': retry_after} if retry_after else {}, None)


class EventTests(ClaudeResilienceBase):
    def test_live_transition_endpoint_windows_then_event_without_utilization_keeps_every_percentage(self):
        state = self.claude_state()
        reset5, reset7 = time.time() + 3 * 3600, time.time() + 4 * 86400
        self.assertTrue(self.endpoint(state, {'five_hour': {'utilization': 37.0, 'resets_at': iso(reset5)},
                                              'seven_day': {'utilization': 12.0, 'resets_at': iso(reset7)}}))
        before = self.windows()['five_hour']['percent_epoch']
        # What the owner's file showed: one allowed event, no utilization, a reset time that differs slightly from the ISO one.
        self.providers.usage.claude_events([event('five_hour', None, reset5 + 90)], state)
        windows = self.windows()
        self.assertEqual(set(windows), {'five_hour', 'seven_day'})
        self.assertEqual((windows['five_hour']['used_percent'], windows['seven_day']['used_percent']), (37, 12))
        self.assertEqual(windows['five_hour']['status'], 'allowed')
        self.assertAlmostEqual(windows['five_hour']['resets_at'], reset5 + 90, delta=1)
        self.assertEqual(windows['five_hour']['percent_epoch'], before)   # the percentage keeps its own measurement time
        self.assertFalse(windows['five_hour'].get('estimated'))
        self.assertEqual(self.providers.usage.public('claude')['source'], 'Claude Code rate_limit_event')

    def test_event_with_utilization_still_replaces_the_percentage(self):
        state = self.claude_state()
        reset = time.time() + 3600
        self.providers.usage.claude_events([event('five_hour', 0.2, reset)], state)
        self.providers.usage.claude_events([event('five_hour', None, reset)], state)
        self.providers.usage.claude_events([event('five_hour', 0.55, reset)], state)
        self.assertEqual(self.windows()['five_hour']['used_percent'], 55)

    def test_new_window_after_the_old_one_ended_starts_at_zero(self):
        state = self.claude_state()
        old_reset = time.time() - 60
        self.providers.usage.save('claude', state['fingerprint'], [{'id': 'five_hour', 'used_percent': 88, 'resets_at': old_reset, 'window_minutes': 300}], 'test')
        self.providers.usage.claude_events([event('five_hour', None, time.time() + 5 * 3600)], state)
        window = self.windows()['five_hour']
        self.assertEqual(window['used_percent'], 0)
        self.assertFalse(window['expired'])
        self.assertFalse(window.get('estimated'))

    def test_new_window_while_the_old_one_is_still_open_keeps_the_value_marked_estimated(self):
        state = self.claude_state()
        old_reset = time.time() + 1800
        self.providers.usage.save('claude', state['fingerprint'], [{'id': 'five_hour', 'used_percent': 64, 'resets_at': old_reset, 'window_minutes': 300}], 'test')
        self.providers.usage.claude_events([event('five_hour', None, old_reset + 3 * 3600)], state)
        window = self.windows()['five_hour']
        self.assertEqual(window['used_percent'], 64)
        self.assertTrue(window['estimated'])
        self.assertTrue(window['stale'] and window['aged'])

    def test_event_kinds_map_to_the_endpoint_ids_and_merge_instead_of_duplicating(self):
        state = self.claude_state()
        reset = time.time() + 3600
        self.endpoint(state, {'five_hour': {'utilization': 10.0, 'resets_at': iso(reset)}, 'seven_day': {'utilization': 20.0, 'resets_at': iso(reset + 86400)},
                              'seven_day_opus': {'utilization': 5.0, 'resets_at': iso(reset + 86400)}})
        self.providers.usage.claude_events([event('session', 0.3, reset), event('weekly', 0.4, reset + 86400), event('seven_day_opus', 0.06, reset + 86400)], state)
        windows = self.windows()
        self.assertEqual(sorted(windows), ['five_hour', 'seven_day', 'seven_day_opus'])
        self.assertEqual([windows[k]['used_percent'] for k in sorted(windows)], [30, 40, 6])

    def test_rejected_event_without_utilization_is_full(self):
        state = self.claude_state()
        self.providers.usage.claude_events([event('five_hour', None, time.time() + 600, 'rejected')], state)
        self.assertEqual(self.windows()['five_hour']['used_percent'], 100)

    def test_event_with_no_earlier_value_stays_unknown_rather_than_invented(self):
        state = self.claude_state()
        self.providers.usage.claude_events([event('five_hour', None, time.time() + 600)], state)
        window = self.windows()['five_hour']
        self.assertIsNone(window['used_percent'])
        self.assertEqual(window['status'], 'allowed')


class MergeTests(ClaudeResilienceBase):
    def test_endpoint_answer_without_the_five_hour_window_merges(self):
        state = self.claude_state()
        reset = time.time() + 3600
        self.endpoint(state, {'five_hour': {'utilization': 10.0, 'resets_at': iso(reset)}, 'seven_day': {'utilization': 20.0, 'resets_at': iso(reset + 86400)}})
        self.endpoint(state, {'seven_day': {'utilization': 25.0, 'resets_at': iso(reset + 86400)}})
        windows = self.windows()
        self.assertEqual((windows['five_hour']['used_percent'], windows['seven_day']['used_percent']), (10, 25))
        self.assertTrue(windows['five_hour']['retained'])

    def test_endpoint_answer_with_the_five_hour_window_replaces_the_set(self):
        state = self.claude_state()
        reset = time.time() + 3600
        self.endpoint(state, {'five_hour': {'utilization': 10.0, 'resets_at': iso(reset)}, 'seven_day_sonnet': {'utilization': 20.0, 'resets_at': iso(reset)}})
        self.endpoint(state, {'five_hour': {'utilization': 11.0, 'resets_at': iso(reset)}})
        self.assertEqual(sorted(self.windows()), ['five_hour'])

    def test_merge_accepts_a_claude_cache_without_identity_info_but_never_mixes_codex_accounts(self):
        usage = self.providers.usage
        reset = time.time() + 3600
        usage.save('claude', 'old-fingerprint', [{'id': 'seven_day', 'used_percent': 33, 'resets_at': reset + 86400}], 'test', identity_key=None)
        usage.cache['claude'].pop('identity_key', None)
        usage.save('claude', 'new-fingerprint', [{'id': 'five_hour', 'used_percent': 5, 'resets_at': reset}], 'event', merge=True, identity_key=None)
        self.assertEqual(sorted(w['id'] for w in usage.cache['claude']['windows']), ['five_hour', 'seven_day'])
        usage.save('codex', 'account-a', [{'id': 'codex:primary', 'used_percent': 50}], 'test')
        usage.save('codex', 'account-b', [{'id': 'codex:secondary', 'used_percent': 5}], 'test', merge=True)
        self.assertEqual([w['id'] for w in usage.cache['codex']['windows']], ['codex:secondary'])

    def test_merge_with_two_different_known_claude_identities_does_not_mix(self):
        usage = self.providers.usage
        usage.save('claude', 'fp-a', [{'id': 'seven_day', 'used_percent': 33}], 'test', identity_key='uuid-a')
        usage.save('claude', 'fp-b', [{'id': 'five_hour', 'used_percent': 5}], 'event', merge=True, identity_key='uuid-b')
        self.assertEqual([w['id'] for w in usage.cache['claude']['windows']], ['five_hour'])


class BackoffTests(ClaudeResilienceBase):
    def test_retry_after_parsing(self):
        self.assertEqual(parse_retry_after('120'), 120)
        self.assertEqual(parse_retry_after(' 7.5 '), 7.5)
        when = datetime.now(timezone.utc) + timedelta(seconds=900)
        self.assertAlmostEqual(parse_retry_after(format_datetime(when, usegmt=True)), 900, delta=3)
        self.assertEqual(parse_retry_after(format_datetime(when - timedelta(hours=1), usegmt=True)), 0)   # a date in the past
        self.assertEqual(parse_retry_after('Wed, 21 Oct 2026 07:28:00 GMT', current=datetime(2026, 10, 21, 7, 20, tzinfo=timezone.utc).timestamp()), 480)
        for bad in (None, '', 'soon', '-5', 'nan', 'inf'):
            self.assertIsNone(parse_retry_after(bad))

    def test_429_honours_retry_after_seconds_and_http_date_else_uses_the_ladder(self):
        state = self.claude_state()
        usage = self.providers.usage
        self.endpoint(state, error=self.rate_limited('90'))
        self.assertAlmostEqual(usage.backoff['claude'] - time.monotonic(), 90, delta=3)
        usage.backoff['claude'] = 0
        usage.attempts.clear()
        when = datetime.now(timezone.utc) + timedelta(seconds=1500)
        self.endpoint(state, error=self.rate_limited(format_datetime(when, usegmt=True)))
        self.assertAlmostEqual(usage.backoff['claude'] - time.monotonic(), 1500, delta=5)
        usage.backoff['claude'] = 0
        usage.attempts.clear()
        self.endpoint(state, error=self.rate_limited())   # third 429 in a row, no header: 5 -> 10 -> 20 minutes
        self.assertAlmostEqual(usage.backoff['claude'] - time.monotonic(), 1200, delta=5)
        self.assertEqual((BACKOFF_MIN, BACKOFF_MAX), (300, 3600))

    def test_backoff_and_count_survive_a_new_usage_instance_and_clear_on_success(self):
        state = self.claude_state()
        self.endpoint(state, error=self.rate_limited())
        self.providers.usage.backoff['claude'] = 0
        self.providers.usage.attempts.clear()
        self.endpoint(state, error=self.rate_limited())
        saved = json.loads(self.providers.usage.path.read_text(encoding='utf-8'))['_backoff']['claude']
        self.assertEqual(saved['count'], 2)
        self.assertAlmostEqual(saved['until'] - time.time(), 600, delta=5)
        restarted = Usage(self.providers)   # a new process reads provider-usage.json
        self.assertAlmostEqual(restarted.backoff['claude'] - time.monotonic(), 600, delta=5)
        self.assertEqual(restarted.backoff_count['claude'], 2)
        self.providers.usage = restarted
        opener = Mock()
        with patch('agentflow.usage.urllib.request.build_opener', return_value=opener):
            self.assertFalse(restarted.refresh_claude(state, force=True))
        opener.open.assert_not_called()   # no request right after a restart while backing off
        restarted.backoff['claude'] = 0
        restarted.attempts.clear()
        self.assertTrue(self.endpoint(state, {'five_hour': {'utilization': 9.0, 'resets_at': iso(time.time() + 3600)}}))
        self.assertNotIn('_backoff', json.loads(restarted.path.read_text(encoding='utf-8')))
        self.assertNotIn('claude', Usage(self.providers).backoff)

    def test_an_expired_stored_backoff_is_not_restored_but_the_ladder_position_is(self):
        usage = self.providers.usage
        usage.cache['_backoff'] = {'claude': {'until': time.time() - 10, 'count': 3}}
        usage.persist()
        restarted = Usage(self.providers)
        self.assertNotIn('claude', restarted.backoff)
        self.assertEqual(restarted.backoff_count['claude'], 3)
        usage.cache['_backoff'] = 'garbage'
        usage.persist()
        self.assertEqual(Usage(self.providers).backoff, {})

    def test_rate_limiting_is_quiet_in_the_public_view(self):
        state = self.claude_state()
        self.providers.usage.save('claude', state['fingerprint'], [{'id': 'five_hour', 'used_percent': 41, 'resets_at': time.time() + 1000, 'window_minutes': 300}], 'test')
        self.endpoint(state, error=self.rate_limited('600'))
        public = self.providers.usage.public('claude')
        self.assertEqual((public['notice'], public['state'], public['windows'][0]['used_percent']), ('', 'observed', 41))
        self.assertTrue(public['rate_limited'])
        self.assertAlmostEqual(public['next_check_epoch'] - time.time(), 600, delta=5)
        self.assertIn('rate-limited', public['rate_notice'])
        self.providers.usage.backoff.pop('claude')
        quiet = self.providers.usage.public('claude')
        self.assertEqual((quiet['rate_limited'], quiet['next_check_epoch'], quiet['rate_notice']), (False, None, ''))


class CadenceAndStartupTests(ClaudeResilienceBase):
    def test_claude_polling_intervals_are_slow_and_codex_is_unchanged(self):
        from agentflow.providers import Providers
        self.assertEqual((Providers.USAGE_ACTIVE['claude'], Providers.USAGE_IDLE['claude']), (600, 1200))
        self.assertEqual((Providers.USAGE_ACTIVE['codex'], Providers.USAGE_IDLE['codex']), (60, 300))

    def plan(self, provider, attempt, clock, polled=True):
        base = {'identity_at': clock - 10, 'usage_at': clock - 10, 'revision': 'r', 'marks': 'm'}
        return self.providers._probe_plan(provider, {**base, **attempt}, 'r', 'm', clock, polled)

    def test_one_refresh_two_minutes_after_an_inference_for_claude_only(self):
        clock = time.monotonic()
        self.providers.usage.pending_inference['claude'] = clock - 30
        plan, delay = self.plan('claude', {'usage_at': clock - 200, 'identity_at': clock - 200}, clock)
        self.assertIsNone(plan)
        self.assertAlmostEqual(delay, 90, delta=1)
        plan, delay = self.plan('claude', {'usage_at': clock - 200, 'identity_at': clock - 200}, clock + 95)
        self.assertTrue(plan['usage'])
        self.providers.usage.pending_inference['codex'] = clock - 30
        plan, _ = self.plan('codex', {'usage_at': clock - 200, 'identity_at': clock - 200}, clock)
        self.assertTrue(plan['usage'])   # codex keeps its short debounce

    def test_no_follow_up_refresh_when_an_event_updated_the_windows_within_five_minutes(self):
        state = self.claude_state()
        self.providers.usage.claude_events([event('five_hour', 0.4, time.time() + 3600)], state)
        clock = time.monotonic()
        self.providers.usage.pending_inference['claude'] = clock - 130
        plan, delay = self.plan('claude', {'usage_at': clock - 200, 'identity_at': clock - 200}, clock)
        self.assertEqual((plan, delay), (None, None))
        self.assertNotIn('claude', self.providers.usage.pending_inference)
        self.providers.usage.event_at['claude'] = clock - 400   # the event is old: the inference may refresh
        self.providers.usage.pending_inference['claude'] = clock - 130
        plan, _ = self.plan('claude', {'usage_at': clock - 200, 'identity_at': clock - 200}, clock)
        self.assertTrue(plan['usage'])

    def test_persisted_windows_are_visible_right_after_a_restart_before_any_probe(self):
        state = self.claude_state()
        self.endpoint(state, {'five_hour': {'utilization': 37.0, 'resets_at': iso(time.time() + 3600)}, 'seven_day': {'utilization': 12.0, 'resets_at': iso(time.time() + 86400)}})
        restarted = Usage(self.providers)
        self.providers.usage = restarted
        self.providers.accounts.current.pop('claude', None)   # nothing confirmed yet in this process
        self.providers.accounts.persisted['claude'] = state
        public = restarted.public('claude', pending=True)
        self.assertEqual(sorted(w['id'] for w in public['windows']), ['five_hour', 'seven_day'])
        self.assertEqual(public['state'], 'observed')
        self.assertTrue(public['updating'])

    def test_codex_windows_for_the_last_confirmed_fingerprint_are_also_visible_at_startup(self):
        state = self.codex_state()
        self.providers.usage.save('codex', state['fingerprint'], [{'id': 'codex:primary', 'used_percent': 20, 'resets_at': time.time() + 600, 'window_minutes': 300}], 'test')
        restarted = Usage(self.providers)
        self.providers.accounts.current.pop('codex')
        self.providers.accounts.persisted['codex'] = state
        self.assertEqual(restarted.public('codex', pending=True)['windows'][0]['used_percent'], 20)
        self.providers.accounts.persisted['codex'] = {**state, 'fingerprint': 'someone-else'}
        self.assertEqual(restarted.public('codex', pending=True)['windows'], [])

    def codex_state(self):
        (self.profile / 'auth.json').write_text(json.dumps({'tokens': {'account_id': 'workspace-a', 'access_token': 'one'}}))
        with patch.object(self.providers, 'read_account', return_value={'type': 'chatgpt', 'email': 'a@example.test', 'planType': 'plus'}):
            return self.providers.accounts.check('codex', force=True)

    def test_windows_age_into_stale_after_fifteen_minutes_and_expired_windows_read_zero(self):
        state = self.claude_state()
        usage = self.providers.usage
        usage.save('claude', state['fingerprint'], [{'id': 'five_hour', 'used_percent': 40, 'resets_at': time.time() + 3600}], 'test')
        fresh = usage.public('claude')['windows'][0]
        self.assertEqual((fresh['stale'], fresh['aged']), (False, False))
        usage.cache['claude']['windows'][0]['percent_epoch'] = time.time() - 600   # 10 minutes: stale for quota gating, not yet "aged" in the UI
        middle = usage.public('claude')['windows'][0]
        self.assertEqual((middle['stale'], middle['aged']), (True, False))
        usage.cache['claude']['windows'][0]['percent_epoch'] = time.time() - 1000
        self.assertTrue(usage.public('claude')['windows'][0]['aged'])
        usage.cache['claude']['windows'][0]['resets_at'] = time.time() - 5
        expired = usage.public('claude')['windows'][0]
        self.assertEqual((expired['expired'], expired['used_percent']), (True, 0))


if __name__ == '__main__':
    unittest.main()
