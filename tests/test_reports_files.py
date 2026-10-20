"""Sprint 2 / agent R: owner attachments, safe deletion (trash), workspace-hygiene guide, Claude usage reliability."""
import io
import json
import os
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

import test_runtime as runtime
from test_usage_housekeeping import UsageBase
from agentflow.contract import SYSTEM_PROMPT, TOOL_SCHEMAS
from agentflow.store import DomainError
from agentflow.usage import CLAUDE_CHECKING_NOTICE, CLAUDE_RATE_NOTICE, CLAUDE_TOKEN_NOTICE, BACKOFF_MAX, BACKOFF_MIN

ROOT = Path(__file__).resolve().parents[1]


class FilesBase(unittest.TestCase):
    setUp = runtime.RuntimeTests.setUp
    tearDown = runtime.RuntimeTests.tearDown
    ready = runtime.RuntimeTests.ready

    def newrun(self, text='Write the quarterly report'):
        self.ready()
        run = self.engine.chat({'message': text, 'agent_id': 'a-lead', 'channel': 'dm-a-lead'})
        return self.store.get('runs', run['id'])

    def agent(self, agent_id='a-lead'):
        return self.store.get('agents', agent_id)

    def call(self, name, args, run):
        return self.engine.tool(self.agent(), {'name': name, 'arguments': args}, run)

    def write(self, path, content='# Report\n\nBody', run=None):
        return self.engine.files.write({'path': path, 'content': content}, self.agent(), run or self.run)

    def turn(self, run, *envelopes):
        with patch.object(self.providers, 'infer', side_effect=[json.dumps(e) for e in envelopes]):
            for _ in envelopes:
                self.engine.execute(run['id'])
        return self.store.get('runs', run['id'])

    def messages(self, run):
        return [m for m in self.store.all('messages') if m.get('run_id') == run['id'] and m.get('role') == 'assistant']


class AttachmentTests(FilesBase):
    def setUp(self):
        super().setUp()
        self.run = self.newrun()

    def test_envelope_files_attach_registered_artifacts_to_the_final_message(self):
        self.write('projects/q3/reports/final.md')
        (self.engine.workspace / 'projects/q3/data.csv').write_text('a,b\n1,2\n')  # exists but never registered
        final = self.turn(self.run, {'reply': 'Done, report attached.', 'state': 'complete', 'summary': 'Report written.',
                                     'files': ['projects/q3/reports/final.md', 'projects/q3/data.csv']})
        self.assertEqual(final['status'], 'done')
        message = next(m for m in self.messages(self.run) if m['kind'] == 'chat')
        self.assertEqual(len(message['artifact_ids']), 2)
        artifacts = [self.store.get('artifacts', i) for i in message['artifact_ids']]
        self.assertEqual({a['path'] for a in artifacts}, {'projects/q3/reports/final.md', 'projects/q3/data.csv'})
        self.assertTrue(all(a['sha256'] and a['source'] == 'workspace' for a in artifacts))
        preview = self.engine.outputs.preview('artifact', message['artifact_ids'][0])
        self.assertEqual(preview['mode'], 'text')

    def test_files_wait_for_pending_tool_calls_then_attach_to_the_final_reply(self):
        final = self.turn(self.run,
            {'reply': 'Writing it now.', 'state': 'continue', 'files': ['reports/late.md'],
             'tool_calls': [{'name': 'workspace.write', 'arguments': {'path': 'reports/late.md', 'content': '# Late'}}]},
            {'reply': 'Finished.', 'state': 'complete', 'summary': 'ok'})
        self.assertEqual(final['status'], 'done')
        first, last = [m for m in self.messages(self.run)][:2]
        self.assertNotIn('artifact_ids', first)
        self.assertEqual(len(last['artifact_ids']), 1)

    def test_missing_and_denied_files_become_a_checkpoint_note_not_a_crash(self):
        self.write('reports/ok.md')
        (self.engine.workspace / '.env').write_text('SECRET=1')
        final = self.turn(self.run, {'reply': 'Done.', 'state': 'complete', 'summary': 'ok',
                                     'files': ['reports/ok.md', 'reports/missing.md', '../escape.md', '.env', '/etc/passwd', 'tool.exe']})
        self.assertEqual(final['status'], 'done')
        message = next(m for m in self.messages(self.run) if m['kind'] == 'chat')
        self.assertEqual(len(message['artifact_ids']), 1)
        note = [t for t in final['trace'] if t['role'] == 'tool'][-1]['content']
        for name in ('missing.md', 'escape.md', '.env', 'passwd', 'tool.exe'):
            self.assertIn(name, note)

    def test_chat_send_files_attach_and_report_errors(self):
        self.write('reports/a.md')
        result = self.call('chat.send', {'text': 'Here it is', 'files': ['reports/a.md', 'reports/none.md'], 'audience': 'owner'}, self.run)
        message = self.store.get('messages', result['message_id'])
        self.assertEqual(message['artifact_ids'], result['artifact_ids'])
        self.assertEqual(len(message['artifact_ids']), 1)
        self.assertIn('none.md', result['attachment_errors'][0])
        plain = self.call('chat.send', {'text': 'No files'}, self.run)
        self.assertNotIn('artifact_ids', self.store.get('messages', plain['message_id']))

    def test_at_most_ten_files_and_customer_runs_never_attach(self):
        for i in range(12):
            self.write(f'reports/r{i}.md', str(i))
        result = self.call('chat.send', {'text': 'Many', 'files': [f'reports/r{i}.md' for i in range(12)]}, self.run)
        self.assertEqual(len(result['artifact_ids']), 10)
        self.assertIn('Only the first 10', result['attachment_errors'][-1])
        support = {**self.run, 'kind': 'support'}
        result = self.engine.team.invoke('chat.send', {'text': 'Hi', 'files': ['reports/r0.md']}, self.agent(), support)
        self.assertNotIn('artifact_ids', result)

    def test_project_folder_files_attach_through_the_grant(self):
        folder = self.root / 'repo'
        (folder / 'docs').mkdir(parents=True)
        (folder / 'docs' / 'notes.md').write_text('# Notes')
        (folder / 'docs' / 'binary.exe').write_text('x')
        self.store.patch('projects', self.run['project_id'], {'folder_grants': [
            {'id': 'g1', 'path': str(folder), 'access': 'read', 'target_type': 'folder'}]})
        result = self.engine.outputs.attach(['docs/notes.md', str(folder / 'docs' / 'notes.md'), 'docs/binary.exe', '../x.md'], self.agent(), self.run)
        self.assertEqual(len(result['artifact_ids']), 1)  # both spellings are the same file
        artifact = self.store.get('artifacts', result['artifact_ids'][0])
        self.assertEqual((artifact['source'], artifact['grant_id'], artifact['path']), ('project', 'g1', 'docs/notes.md'))
        self.assertEqual(self.engine.outputs.preview('artifact', artifact['id'])['content'], '# Notes')
        self.assertEqual(len(result['notes']), 2)
        other = self.store.create('projects', {'title': 'No grants', 'folder_grants': []})
        self.assertEqual(self.engine.outputs.attach(['docs/notes.md'], self.agent(), {**self.run, 'project_id': other['id']})['artifact_ids'], [])

    def test_attaching_a_file_the_owner_made_does_not_make_it_deletable(self):
        old = self.engine.workspace / 'owner-notes.md'
        old.write_text('mine')
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        self.assertEqual(len(self.engine.outputs.attach(['owner-notes.md'], self.agent(), self.run)['artifact_ids']), 1)
        with self.assertRaises(DomainError):
            self.call('workspace.delete', {'paths': ['owner-notes.md']}, self.run)
        self.assertTrue(old.exists())

    def test_contract_documents_files_and_delete_tools(self):
        self.assertIn('"files"', SYSTEM_PROMPT)
        self.assertIn('files', TOOL_SCHEMAS['chat.send'])
        self.assertIn('workspace.delete', TOOL_SCHEMAS)
        self.assertIn('project.delete', TOOL_SCHEMAS)


class TelegramTests(FilesBase):
    def setUp(self):
        super().setUp()
        self.run = self.newrun()
        self.vault.set({'TELEGRAM_BOT_TOKEN': 'bot-token'})
        self.outbound = {'channel': 'telegram', 'recipient': '42', 'event_key': 'evt-1'}

    def fake_open(self, requests):
        def opener(request, timeout=0):
            requests.append(request)
            return io.BytesIO(b'{"ok":true}')
        return opener

    def artifact(self, path='reports/final.md', content='# Final report'):
        self.write(path, content)
        return self.engine.outputs.attach([path], self.agent(), self.run)['artifact_ids'][0]

    def test_documents_are_sent_with_multipart_after_the_text(self):
        artifact_id = self.artifact()
        requests = []
        with patch('urllib.request.urlopen', side_effect=self.fake_open(requests)):
            self.assertEqual(self.engine.send_reply(self.outbound, 'Report ready', [artifact_id]), 'sent')
        self.assertTrue(requests[0].full_url.endswith('/sendMessage'))
        self.assertEqual(json.loads(requests[0].data)['text'], 'Report ready')
        document = requests[1]
        self.assertTrue(document.full_url.endswith('/sendDocument'))
        self.assertIn('multipart/form-data; boundary=', document.get_header('Content-type'))
        self.assertIn(b'filename="final.md"', document.data)
        self.assertIn(b'# Final report', document.data)
        self.assertIn(b'name="chat_id"\r\n\r\n42', document.data)

    def test_large_files_are_mentioned_not_uploaded(self):
        artifact_id = self.artifact()
        requests = []
        with patch.object(type(self.engine), 'TELEGRAM_FILE_LIMIT', 5), patch('urllib.request.urlopen', side_effect=self.fake_open(requests)):
            self.engine.send_reply({**self.outbound, 'event_key': 'evt-2'}, 'Report ready', [artifact_id])
        self.assertEqual(len(requests), 1)
        self.assertIn('reports/final.md', json.loads(requests[0].data)['text'])

    def test_a_failed_document_does_not_mark_the_text_as_unconfirmed(self):
        artifact_id = self.artifact()
        calls = []
        def opener(request, timeout=0):
            calls.append(request.full_url)
            if request.full_url.endswith('sendDocument'):
                raise urllib.error.HTTPError(request.full_url, 400, 'Bad', {}, None)
            return io.BytesIO(b'{"ok":true}')
        with patch('urllib.request.urlopen', side_effect=opener):
            self.assertEqual(self.engine.send_reply({**self.outbound, 'event_key': 'evt-3'}, 'Hi', [artifact_id]), 'sent')
        self.assertEqual(len(calls), 2)

    def test_emit_queues_attachments_for_telegram_and_the_outbox_delivers_them(self):
        artifact_id = self.artifact()
        run = {**self.run, 'outbound': self.outbound}
        message = self.engine.team.emit('a-lead', 'Report ready', run, artifact_ids=[artifact_id])
        item = next(i for i in self.store.all('channel_outbox') if i['outbound']['event_key'] == 'message:' + message['id'])
        self.assertEqual(item['artifact_ids'], [artifact_id])
        requests = []
        with patch('urllib.request.urlopen', side_effect=self.fake_open(requests)):
            self.engine.deliver_outbox(item)
        self.assertEqual([r.full_url.rsplit('/', 1)[1] for r in requests], ['sendMessage', 'sendDocument'])
        self.assertEqual(self.store.get('channel_outbox', item['id'])['status'], 'sent')


class DeleteTests(FilesBase):
    def setUp(self):
        super().setUp()
        self.run = self.newrun()

    def trash_entries(self):
        return list((self.store.directory / 'trash').glob('*/*/*/manifest.json'))

    def test_owner_files_are_refused_and_nothing_is_moved(self):
        owner = self.engine.workspace / 'owner.md'
        owner.write_text('mine')
        mine = self.write('reports/mine.md')
        with self.assertRaises(DomainError) as refused:
            self.call('workspace.delete', {'paths': ['reports/mine.md', 'owner.md']}, self.run)
        self.assertEqual(refused.exception.status, 403)
        self.assertIn('owner.md', str(refused.exception))
        self.assertTrue(owner.exists() and (self.engine.workspace / 'reports/mine.md').exists())
        self.assertEqual(self.trash_entries(), [])
        self.assertTrue(mine['id'])

    def test_a_file_an_agent_only_modified_is_still_the_owners(self):
        owner = self.engine.workspace / 'brief.md'
        owner.write_text('original')
        self.write('brief.md', 'edited by agent')
        with self.assertRaises(DomainError):
            self.call('workspace.delete', {'path': 'brief.md'}, self.run)
        self.assertTrue(owner.exists())

    def test_agent_file_moves_to_trash_with_manifest_and_can_be_restored(self):
        saved = self.write('projects/x/reports/draft.md', 'draft text')
        result = self.call('workspace.delete', {'path': 'projects/x/reports/draft.md'}, self.run)
        self.assertEqual(result['deleted'], ['projects/x/reports/draft.md'])
        self.assertFalse((self.engine.workspace / 'projects/x/reports/draft.md').exists())
        manifest_path, = self.trash_entries()
        self.assertEqual(manifest_path.parents[2].name, time.strftime('%Y%m%d'))
        self.assertEqual(manifest_path.parents[1].name, self.run['id'])
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        self.assertEqual((manifest['run_id'], manifest['agent_id'], manifest['source']), (self.run['id'], 'a-lead', 'workspace'))
        self.assertEqual(manifest['items'][0]['original_path'], 'projects/x/reports/draft.md')
        self.assertEqual((manifest_path.parent / 'files/projects/x/reports/draft.md').read_text(), 'draft text')
        self.assertEqual(self.store.get('artifacts', saved['id'])['trash_id'], result['trash_id'])
        restored = self.engine.files.restore(result['trash_id'])
        self.assertEqual(restored['restored'], ['projects/x/reports/draft.md'])
        self.assertEqual((self.engine.workspace / 'projects/x/reports/draft.md').read_text(), 'draft text')
        self.assertIsNone(self.store.get('artifacts', saved['id'])['trash_id'])
        self.assertEqual(self.trash_entries(), [])
        with self.assertRaises(DomainError):
            self.engine.files.restore(result['trash_id'])

    def test_restore_never_overwrites(self):
        self.write('a.md', 'one')
        result = self.call('workspace.delete', {'path': 'a.md'}, self.run)
        (self.engine.workspace / 'a.md').write_text('new owner file')
        with self.assertRaises(DomainError) as blocked:
            self.engine.files.restore(result['trash_id'])
        self.assertEqual(blocked.exception.status, 409)
        self.assertEqual((self.engine.workspace / 'a.md').read_text(), 'new owner file')

    def test_scratch_files_and_folders_qualify_even_when_made_by_a_script(self):
        scratch = self.engine.workspace / 'projects/x/scratch/tmp'
        scratch.mkdir(parents=True)
        (scratch / 'dump.json').write_text('{}')
        (self.engine.workspace / 'projects/x/scratch/out.png').write_bytes(b'png')
        result = self.call('workspace.delete', {'paths': ['projects/x/scratch']}, self.run)
        self.assertEqual(result['deleted'], ['projects/x/scratch'])
        self.assertFalse((self.engine.workspace / 'projects/x/scratch').exists())
        self.assertEqual(self.engine.files.restore(result['trash_id'])['restored'], ['projects/x/scratch'])
        self.assertTrue((scratch / 'dump.json').exists())

    def test_folders_need_every_file_to_qualify(self):
        self.write('projects/y/reports/mine.md')
        (self.engine.workspace / 'projects/y/reports/owner.md').write_text('keep')
        with self.assertRaises(DomainError):
            self.call('workspace.delete', {'path': 'projects/y/reports'}, self.run)
        self.assertTrue((self.engine.workspace / 'projects/y/reports/mine.md').exists())
        (self.engine.workspace / 'projects/y/reports/owner.md').unlink()
        self.assertEqual(self.call('workspace.delete', {'path': 'projects/y/reports'}, self.run)['deleted'], ['projects/y/reports'])

    def test_root_traversal_secrets_and_missing_paths_are_refused(self):
        self.write('reports/a.md')
        for bad in ('', '.', '../x.md', '.env', str(self.root / 'x.md')):
            with self.assertRaises(DomainError):
                self.call('workspace.delete', {'path': bad}, self.run)
        with self.assertRaises(DomainError):
            self.call('workspace.delete', {'path': 'reports/never-existed.md'}, self.run)
        self.assertTrue((self.engine.workspace / 'reports/a.md').exists())

    def test_symlinks_are_never_followed(self):
        if os.name == 'nt':
            self.skipTest('symlinks need privileges on Windows')
        outside = self.root / 'outside.md'
        outside.write_text('x')
        scratch = self.engine.workspace / 'scratch'
        scratch.mkdir()
        os.symlink(outside, scratch / 'link.md')
        with self.assertRaises(DomainError):
            self.call('workspace.delete', {'path': 'scratch'}, self.run)
        self.assertTrue(outside.exists())

    def test_project_delete_follows_the_folder_grant(self):
        folder = self.root / 'repo'
        folder.mkdir()
        (folder / 'owner.py').write_text('print(1)')
        grant = {'id': 'g1', 'path': str(folder), 'access': 'read', 'target_type': 'folder'}
        project = self.store.patch('projects', self.run['project_id'], {'folder_grants': [grant]})
        denied = self.call('project.delete', {'project_id': project['id'], 'path': 'owner.py'}, self.run)
        self.assertTrue(denied.get('waiting_permission') or denied.get('status') in ('open', 'pending') or 'permission' in json.dumps(denied).lower())
        self.assertTrue((folder / 'owner.py').exists())
        self.store.patch('projects', project['id'], {'folder_grants': [{**grant, 'access': 'readwrite'}]})
        # Owner decision: a readwrite grant covers deleting existing owner files (moved to the restorable trash).
        removed = self.call('project.delete', {'project_id': project['id'], 'path': 'owner.py'}, self.run)
        self.assertEqual(removed['deleted'], ['owner.py'])
        self.assertFalse((folder / 'owner.py').exists())
        self.engine.files.restore(removed['trash_id'])
        self.assertTrue((folder / 'owner.py').exists())
        self.engine.files.write({'project_id': project['id'], 'path': 'notes/tmp.md', 'content': 'tmp'}, self.agent(), self.run, project=True)
        result = self.call('project.delete', {'project_id': project['id'], 'path': 'notes/tmp.md'}, self.run)
        self.assertEqual(result['deleted'], ['notes/tmp.md'])
        self.assertFalse((folder / 'notes/tmp.md').exists())
        self.assertTrue((folder / 'owner.py').exists())
        manifest = json.loads(self.trash_entries()[-1].read_text(encoding='utf-8'))
        self.assertEqual((manifest['source'], manifest['project_id'], manifest['root']), ('project', project['id'], str(folder.resolve())))
        self.engine.files.restore(result['trash_id'])
        self.assertEqual((folder / 'notes/tmp.md').read_text(), 'tmp')

    def test_agents_without_the_tool_in_their_list_can_still_delete_their_scratch(self):
        self.store.patch('agents', 'a-lead', {'tools': ['workspace.read']})
        scratch = self.engine.workspace / 'scratch'
        scratch.mkdir()
        (scratch / 'a.txt').write_text('x')
        self.assertEqual(self.call('workspace.delete', {'path': 'scratch/a.txt'}, self.run)['deleted'], ['scratch/a.txt'])

    def test_purge_removes_only_entries_older_than_the_window_and_housekeeping_calls_it(self):
        self.write('old.md')
        self.write('new.md')
        old = self.call('workspace.delete', {'path': 'old.md'}, self.run)
        self.call('workspace.delete', {'path': 'new.md'}, self.run)
        manifest = next(p for p in self.trash_entries() if json.loads(p.read_text())['id'] == old['trash_id'])
        data = json.loads(manifest.read_text())
        data['deleted_epoch'] = time.time() - 8 * 86400
        manifest.write_text(json.dumps(data))
        self.assertEqual(self.engine.files.purge_trash(7)['files'], 1)
        self.assertEqual(len(self.trash_entries()), 1)
        self.assertFalse(manifest.parent.exists())
        # Everything left is recent; housekeeping's automatic cleanup keeps it.
        self.engine.housekeeping.run_auto()
        self.assertEqual(len(self.trash_entries()), 1)
        data = json.loads(self.trash_entries()[0].read_text())
        data['deleted_epoch'] = time.time() - 9 * 86400
        self.trash_entries()[0].write_text(json.dumps(data))
        self.engine.housekeeping.run_auto()
        self.assertEqual(self.trash_entries(), [])

    def test_trash_listing_and_restore(self):
        self.write('route.md', 'x')
        result = self.call('workspace.delete', {'path': 'route.md'}, self.run)
        self.assertEqual([t['id'] for t in self.engine.files.trash_list()], [result['trash_id']])
        self.assertEqual(self.engine.files.restore(result['trash_id'])['trash_id'], result['trash_id'])
        self.assertTrue((self.engine.workspace / 'route.md').exists())


class GuideTests(unittest.TestCase):
    def test_workspace_hygiene_guide_is_compact_english_and_loaded(self):
        text = (ROOT / 'agentflow/guides/workspace-hygiene.md').read_text(encoding='utf-8')
        self.assertLessEqual(len(text), 1800)
        self.assertTrue(text.startswith('---\nname: workspace-hygiene\ndescription:'))
        for needle in ('scratch', 'workspace.delete', '"files"', 'skills.create', 'reports'):
            self.assertIn(needle, text)
        self.assertTrue(text.isascii())


class ClaudeReliabilityTests(UsageBase):
    def seed(self, percent=41, **extra):
        state = self.claude_state()
        self.providers.usage.save('claude', state['fingerprint'], [{'id': 'five_hour', 'used_percent': percent, 'resets_at': time.time() + 1000, 'window_minutes': 300}], 'test', **extra)
        return state

    def refresh(self, state, **options):
        opener = Mock()
        if 'error' in options:
            opener.open.side_effect = options['error']
        else:
            opener.open.return_value = io.StringIO(json.dumps(options['payload']))
        with patch('agentflow.usage.urllib.request.build_opener', return_value=opener):
            return self.providers.usage.refresh_claude(state, force=True), opener

    def rate_limited(self, retry_after=None):
        headers = {'Retry-After': retry_after} if retry_after else {}
        return urllib.error.HTTPError('u', 429, 'Too Many', headers, None)

    def expire_backoff(self):
        self.providers.usage.backoff['claude'] = time.monotonic() - 1
        self.providers.usage.attempts.clear()

    # -- never unavailable while a confirmed observation of the same identity exists -------------
    def test_failed_identity_check_keeps_usage_models_and_retries_in_thirty_seconds(self):
        state = self.seed()
        self.providers.cli['claude'] = 'claude-test.exe'
        self.providers.verified.add('claude:sonnet')
        self.providers.models.append({'provider': 'claude', 'id': 'sonnet', 'account_fingerprint': state['fingerprint']})
        self.claude_files(token='token-two')
        self.providers.accounts.revisions['claude'] = 'older-revision'   # the revision no longer matches: unconfirmed
        with patch.object(self.providers, 'read_account', side_effect=DomainError('Unable to read the current Claude Code account through auth status.', 502)):
            self.providers._probe_status('claude', {'identity': True, 'usage': True})
        public = self.providers.usage.public('claude')
        self.assertEqual(public['state'], 'observed')
        self.assertEqual(public['windows'][0]['used_percent'], 41)
        self.assertTrue(public['stale'])
        self.assertIn('last confirmed', public['notice'])
        self.assertIn('claude:sonnet', self.providers.verified)
        self.assertTrue(any(m['provider'] == 'claude' for m in self.providers.models))
        self.assertIn('claude', self.providers.account_errors)
        clock = time.monotonic()
        attempt = {'identity_at': clock - 10, 'usage_at': clock - 10, 'revision': 'r', 'marks': 'm'}
        plan, delay = self.providers._probe_plan('claude', attempt, 'r', 'm', clock, True)
        self.assertIsNone(plan)
        self.assertAlmostEqual(delay, 20, delta=1)
        attempt['identity_at'] = clock - 31
        plan, _ = self.providers._probe_plan('claude', attempt, 'r', 'm', clock, True)
        self.assertTrue(plan['identity'])
        # A second failure doubles the wait.
        self.providers.identity_failures['claude'] = 2
        attempt['identity_at'] = clock - 31
        self.assertEqual(self.providers._probe_plan('claude', attempt, 'r', 'm', clock, True)[0], None)

    def test_unconfirmed_account_state_still_shows_the_same_identity(self):
        self.seed()
        self.providers.accounts.current.pop('claude')           # what a failed re-check does
        public = self.providers.usage.public('claude')
        self.assertEqual((public['state'], public['windows'][0]['used_percent']), ('observed', 41))
        self.assertTrue(public['stale'] and public['updating'])
        self.assertEqual(public['notice'], CLAUDE_CHECKING_NOTICE)
        pending = self.providers.usage.public('claude', pending=True)
        self.assertEqual(pending['windows'][0]['used_percent'], 41)

    def test_torn_reads_of_the_cli_state_files_do_not_look_like_a_new_account(self):
        self.seed()
        revision = self.providers.accounts.revision('claude')
        (self.profile / '.claude.json').write_text('{"oauthAccount": {"accountUuid": "uuid-a", "ema')   # mid-write
        (self.profile / '.credentials.json').write_text('{"claudeAiOauth": {"accessTo')
        self.assertEqual(self.providers.accounts.revision('claude'), revision)
        self.assertEqual(self.providers.usage.public('claude')['windows'][0]['used_percent'], 41)
        self.claude_files(token='token-three')
        self.assertEqual(self.providers.accounts.revision('claude'), revision)
        (self.profile / '.credentials.json').unlink()   # a real sign-out is still visible
        self.assertNotEqual(self.providers.accounts.revision('claude'), revision)

    def test_same_identity_with_a_new_fingerprint_keeps_windows_until_the_next_read(self):
        state = self.seed()
        with patch.object(self.providers, 'read_account', return_value={**self.claude_account(), 'subscription_type': 'pro'}):
            second = self.providers.accounts.check('claude', force=True)
        self.assertNotEqual(second['fingerprint'], state['fingerprint'])
        public = self.providers.usage.public('claude')
        self.assertEqual(public['windows'][0]['used_percent'], 41)
        self.assertEqual(public['notice'], CLAUDE_CHECKING_NOTICE)
        updated, _ = self.refresh(second, payload={'five_hour': {'utilization': 50.0, 'resets_at': None}})
        self.assertTrue(updated)
        public = self.providers.usage.public('claude')
        self.assertEqual(public['windows'][0]['used_percent'], 50)
        self.assertNotEqual(public['notice'], CLAUDE_CHECKING_NOTICE)

    def test_a_different_confirmed_identity_still_clears_usage(self):
        self.seed()
        self.claude_files(uuid='uuid-other', email='other@example.test')
        self.providers.accounts.current.pop('claude')
        public = self.providers.usage.public('claude')
        self.assertEqual((public['windows'], public['state']), ([], 'unavailable'))

    def test_cache_written_before_identity_keys_is_adopted_for_the_same_fingerprint(self):
        state = self.seed()
        self.providers.usage.cache['claude'].pop('identity_key')
        self.providers.accounts.current.pop('claude')
        self.providers.accounts.persisted['claude'] = state
        public = self.providers.usage.public('claude')
        self.assertEqual(public['windows'][0]['used_percent'], 41)
        self.assertIsNotNone(self.providers.usage.cache['claude']['identity_key'])
        self.providers.usage.cache['claude'].pop('identity_key')
        self.providers.accounts.persisted['claude'] = {**state, 'fingerprint': 'someone-else'}
        self.assertEqual(self.providers.usage.public('claude')['windows'], [])

    # -- 429 ---------------------------------------------------------------------------------------
    def test_429_backs_off_five_ten_twenty_forty_minutes_capped_at_sixty_even_when_forced(self):
        state = self.seed()
        usage = self.providers.usage
        delays = []
        for _ in range(6):
            started = time.monotonic()
            updated, _ = self.refresh(state, error=self.rate_limited())
            self.assertFalse(updated)
            delays.append(round(usage.backoff['claude'] - started))
            public = usage.public('claude')
            # Rate limiting is not an error for the owner: no notice, the last value stays, only the next check time is offered.
            self.assertEqual((public['notice'], public['windows'][0]['used_percent'], public['rate_limited']), ('', 41, True))
            self.assertEqual(public['rate_notice'], CLAUDE_RATE_NOTICE)
            self.assertAlmostEqual(public['next_check_epoch'] - time.time(), usage.backoff['claude'] - time.monotonic(), delta=2)
            # While backing off a forced refresh does not even reach the endpoint.
            updated, opener = self.refresh(state, payload={'five_hour': {'utilization': 1.0, 'resets_at': None}})
            self.assertFalse(updated)
            opener.open.assert_not_called()
            self.expire_backoff()
        self.assertEqual(delays, [300, 600, 1200, 2400, BACKOFF_MAX, BACKOFF_MAX])
        self.assertIn('rate-limited; showing last confirmed', CLAUDE_RATE_NOTICE)
        updated, _ = self.refresh(state, payload={'five_hour': {'utilization': 7.0, 'resets_at': None}})
        self.assertTrue(updated)
        self.assertNotIn('claude', usage.backoff_count)
        self.refresh(state, error=self.rate_limited())
        self.assertAlmostEqual(usage.backoff['claude'] - time.monotonic(), BACKOFF_MIN, delta=3)  # the ladder restarts

    def test_retry_after_is_honoured_but_never_beyond_sixty_minutes(self):
        state = self.seed()
        self.refresh(state, error=self.rate_limited('400'))
        self.assertAlmostEqual(self.providers.usage.backoff['claude'] - time.monotonic(), 400, delta=3)
        self.expire_backoff()
        self.refresh(state, error=self.rate_limited('99999'))
        self.assertLessEqual(self.providers.usage.backoff['claude'] - time.monotonic(), BACKOFF_MAX + 1)

    # -- 401 ---------------------------------------------------------------------------------------
    def test_401_is_retried_only_after_the_credentials_change(self):
        state = self.seed()
        updated, _ = self.refresh(state, error=urllib.error.HTTPError('u', 401, 'Unauthorized', {}, None))
        self.assertFalse(updated)
        self.assertEqual(self.providers.usage.public('claude')['notice'], CLAUDE_TOKEN_NOTICE)
        for _ in range(2):
            self.providers.usage.attempts.clear()
            updated, opener = self.refresh(state, payload={'five_hour': {'utilization': 9.0, 'resets_at': None}})
            self.assertFalse(updated)
            opener.open.assert_not_called()
        self.claude_files(token='token-after-cli-refresh')
        self.providers.usage.attempts.clear()
        updated, opener = self.refresh(state, payload={'five_hour': {'utilization': 9.0, 'resets_at': None}})
        self.assertTrue(updated)
        opener.open.assert_called_once()
        self.assertEqual(self.providers.usage.public('claude')['windows'][0]['used_percent'], 9)
        self.assertNotIn('claude', self.providers.usage.auth_hold)

    # -- credentials rewritten by the CLI ------------------------------------------------------------
    def test_credential_change_schedules_a_usage_read_within_five_seconds(self):
        self.seed()
        usage = self.providers.usage
        usage.poll_credentials()          # baseline after the seeded files
        usage.refresh_requested.clear()
        self.assertFalse(usage.poll_credentials())
        self.assertNotIn('claude', usage.refresh_requested)
        self.claude_files(token='token-refreshed')
        before = time.monotonic()
        self.assertTrue(usage.poll_credentials())
        due = usage.refresh_requested['claude']
        self.assertTrue(0 < due - before <= 5)
        clock = time.monotonic()
        attempt = {'identity_at': clock - 5, 'usage_at': clock - 5, 'revision': 'r', 'marks': 'm'}
        plan, delay = self.providers._probe_plan('claude', attempt, 'r', 'm', clock, True)
        self.assertIsNone(plan)
        self.assertTrue(0 < delay <= 5)
        plan, _ = self.providers._probe_plan('claude', attempt, 'r', 'm', due + 0.01, True)
        self.assertTrue(plan['usage'])
        self.assertFalse(plan['identity'])

    def test_schedule_arms_a_short_timer_then_probes_and_clears_the_request(self):
        self.seed()
        self.providers.cli['claude'] = 'claude-test.exe'
        usage = self.providers.usage
        clock = time.monotonic()
        self.providers.status_attempts['claude'] = {'identity_at': clock - 5, 'usage_at': clock - 5,
            'revision': self.providers.accounts.revision('claude'), 'marks': self.providers.accounts.marks('claude')}
        usage.refresh_requested['claude'] = clock + 2
        with patch.object(self.providers, '_arm_timer') as arm, patch.object(self.providers, '_probe_status') as probe:
            self.providers._schedule('claude', polled=True)
        probe.assert_not_called()
        self.assertLessEqual(arm.call_args.args[1], 2.01)
        usage.refresh_requested['claude'] = time.monotonic() - 0.1
        with patch.object(self.providers, '_probe_status') as probe:
            self.assertTrue(self.providers._schedule('claude', polled=True))
            for thread in self.providers.status_threads.values():
                thread.join(timeout=5)
        probe.assert_called_once()
        self.assertNotIn('claude', usage.refresh_requested)

    def test_a_change_arriving_during_a_slow_probe_is_picked_up_right_after(self):
        self.providers.cli['claude'] = 'claude-test.exe'
        import threading
        release = threading.Event()
        def slow(provider, plan=None):
            release.wait(timeout=5)
        with patch.object(self.providers, '_probe_status', side_effect=slow):
            self.assertTrue(self.providers._schedule('claude', polled=True))
            self.providers.usage.refresh_requested['claude'] = time.monotonic() + 1
            with patch.object(self.providers, '_arm_timer') as arm:
                self.assertFalse(self.providers._schedule('claude', polled=True))
            arm.assert_called_once()
            release.set()
            for thread in self.providers.status_threads.values():
                thread.join(timeout=5)

    def test_status_polls_credentials_and_background_tick_kicks_a_refresh(self):
        self.providers.cli['claude'] = 'claude-test.exe'
        self.claude_files()
        with patch.object(self.providers.usage, 'ensure_watch'), patch.object(self.providers, '_schedule') as schedule:
            self.providers.usage.poll_credentials()
            self.claude_files(token='rotated')
            self.providers.last_poll = time.monotonic() - 120
            self.providers.background_tick()
        self.assertIn('claude', self.providers.usage.refresh_requested)
        self.assertIn('claude', {c.args[0] for c in schedule.call_args_list})

    def test_credential_watcher_thread_detects_a_rewrite(self):
        self.providers.cli['claude'] = 'claude-test.exe'
        self.claude_files()
        usage = self.providers.usage
        usage.poll_credentials()
        with patch('agentflow.usage.CREDENTIAL_POLL', 0.05), patch.object(self.providers, '_schedule') as schedule:
            usage.ensure_watch()
            self.claude_files(token='rotated-by-cli')
            deadline = time.time() + 3
            while time.time() < deadline and not schedule.called:
                time.sleep(0.05)
        self.assertTrue(schedule.called)
        self.assertIn('claude', usage.refresh_requested)

    # -- inference rate_limit_event ----------------------------------------------------------------
    def test_inference_rate_limit_events_update_windows_without_touching_the_identity(self):
        state = self.seed()
        reset = time.time() + 600
        events = [{'type': 'rate_limit_event', 'rate_limit_info': {'rateLimitType': 'five_hour', 'utilization': 0.62, 'resetsAt': reset, 'status': 'allowed_warning'}},
                  {'type': 'rate_limit_event', 'rate_limit_info': {'rateLimitType': 'seven_day', 'utilization': 0.9, 'resetsAt': reset + 5000}},
                  {'type': 'assistant'}]
        with patch.object(self.providers.accounts, 'check', side_effect=DomainError('auth status timed out', 502)) as check:
            self.providers.usage.claude_events(events, state)
        check.assert_not_called()
        windows = {w['id']: w for w in self.providers.usage.public('claude')['windows']}
        self.assertEqual((windows['five_hour']['used_percent'], windows['seven_day']['used_percent']), (62, 90))
        self.assertEqual(windows['five_hour']['status'], 'allowed_warning')
        self.assertEqual(windows['seven_day']['window_minutes'], 10080)

    def test_events_flow_through_the_cli_result_parser_and_survive_a_rate_limited_endpoint(self):
        state = self.seed()
        self.providers.usage.backoff['claude'] = time.monotonic() + 600
        lines = [json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {'rateLimitType': 'five_hour', 'utilization': 0.8, 'resetsAt': time.time() + 300}}),
                 json.dumps({'type': 'result', 'result': 'ok', 'session_id': 's1', 'usage': {'input_tokens': 1, 'output_tokens': 1}})]
        self.providers._cli_result('claude', '\n'.join(lines), '', 0, state)
        public = self.providers.usage.public('claude')
        self.assertEqual(public['windows'][0]['used_percent'], 80)
        self.assertFalse(public['windows'][0]['stale'])

    def test_a_rejected_event_without_utilization_reads_as_full(self):
        state = self.seed()
        self.providers.usage.claude_events([{'type': 'rate_limit_event', 'rate_limit_info': {'rateLimitType': 'five_hour', 'status': 'rejected', 'resetsAt': time.time() + 300}}], state)
        self.assertEqual(self.providers.usage.public('claude')['windows'][0]['used_percent'], 100)


if __name__ == '__main__':
    unittest.main()
