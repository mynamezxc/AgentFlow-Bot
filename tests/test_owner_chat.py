import json
import unittest
from unittest.mock import patch

import test_runtime as runtime


class OwnerChatNoiseTests(unittest.TestCase):
    setUp = runtime.RuntimeTests.setUp
    tearDown = runtime.RuntimeTests.tearDown
    ready = runtime.RuntimeTests.ready

    def test_intermediate_narration_is_activity_and_final_reply_is_owner(self):
        self.ready()
        run = self.engine.chat({'message': 'Summarize the brief', 'agent_id': 'a-lead', 'channel': 'dm-a-lead'})
        step = json.dumps({'reply': 'I am reading the brief now.', 'state': 'continue',
                           'tool_calls': [{'name': 'memory.search', 'arguments': {'query': 'brief'}}]})
        final = json.dumps({'reply': 'Here is the summary.', 'state': 'complete', 'summary': 'Summarized the brief.'})
        with patch.object(self.providers, 'infer', side_effect=[step, final]):
            self.engine.execute(run['id'])
        replies = [m for m in self.store.all('messages') if m.get('run_id') == run['id'] and m['role'] == 'assistant']
        audiences = {m['content']: m.get('audience') for m in replies}
        self.assertEqual(audiences['I am reading the brief now.'], 'activity')
        self.assertEqual(audiences['Here is the summary.'], 'owner')
        prompt = self.engine.prompt(self.store.get('agents', 'a-lead'), self.store.get('runs', run['id']))
        self.assertIn('OWNER CHAT SHOWS ONLY WHAT MATTERS', prompt)


if __name__ == '__main__':
    unittest.main()
