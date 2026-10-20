import unittest

import test_runtime as runtime
from agentflow.authoring import optimize_skill, SKILL_LIMIT
from agentflow.language import foreign_ratio, owner_language
from agentflow.prompting import language_line
from agentflow.store import DomainError

GOOD_SKILL = """# Landing page audit

## Purpose
Audit a landing page for clarity and conversion.

## Inputs
- URL or file path

## Steps
1. Open the page and capture desktop and mobile screenshots.
2. List issues by severity.

## Verification
Every issue cites a screenshot.

## Outputs
A Markdown report with a prioritized issue table.
"""


class LanguageTests(unittest.TestCase):
    def test_owner_language_detection_and_continuity(self):
        self.assertEqual(owner_language('Build the launch report'), 'en')
        self.assertEqual(owner_language('Tạo báo cáo giúp tôi'), 'vi')
        self.assertEqual(owner_language('ok tiep tuc', previous='vi'), 'vi')
        self.assertEqual(owner_language('C:\\Projects\\demo-shop', previous='vi'), 'vi')
        self.assertEqual(owner_language('Please write the summary for this report', previous='vi'), 'en')
        self.assertEqual(owner_language('Tạo báo cáo, trả lời bằng tiếng Anh'), 'en')
        self.assertEqual(owner_language('please reply in Japanese'), 'ja')
        self.assertEqual(owner_language('レポートを作成してください'), 'ja')
        self.assertEqual(owner_language('请帮我写报告'), 'zh')

    def test_foreign_ratio_ignores_quoted_owner_data(self):
        self.assertGreater(foreign_ratio('Kiểm tra trang chủ và báo cáo lỗi'), 0.5)
        self.assertEqual(foreign_ratio('Check the hero copy "Bán nhanh, quản lý gọn" for typos'), 0)
        self.assertEqual(foreign_ratio('Run `npm test` in C:/Projects'), 0)

    def test_language_line_internal_vs_owner_runs(self):
        self.assertIn('Owner reply language: vi (Vietnamese)', language_line({'kind': 'chat', 'owner_language': 'vi'}))
        line = language_line({'kind': 'task', 'owner_language': 'vi'})
        self.assertIn('Reply language: English', line)
        self.assertIn('vi (Vietnamese)', line)


class SkillOptimizationTests(unittest.TestCase):
    def test_front_matter_and_advisories(self):
        doc, notes = optimize_skill({'name': 'Landing page audit', 'description': 'Use when auditing a landing page.',
                                     'content': GOOD_SKILL + '\n\n\n\n'}, [])
        self.assertTrue(doc['content'].startswith('---\nname: landing-page-audit\ndescription: Use when auditing a landing page.\n---'))
        self.assertNotIn('\n\n\n', doc['content'])
        self.assertEqual(notes, [])
        _, notes = optimize_skill({'name': 'Quick note', 'description': 'Use for quick notes.', 'content': '# Quick note\nDo it.'}, [])
        self.assertIn('verification', notes[0])

    def test_rejects_long_foreign_and_duplicate_skills(self):
        with self.assertRaises(DomainError):
            optimize_skill({'name': 'Big', 'description': 'Use for big things.', 'content': GOOD_SKILL + 'x ' * SKILL_LIMIT}, [])
        with self.assertRaises(DomainError):
            optimize_skill({'name': 'Kiểm tra trang', 'description': 'Dùng khi kiểm tra', 'content': GOOD_SKILL}, [])
        existing = [{'id': 'skill-1', 'name': 'Landing page audit', 'description': 'Use when auditing a landing page.', 'installed': True}]
        with self.assertRaises(DomainError) as error:
            optimize_skill({'name': 'Landing page audit', 'description': 'Use when auditing landing pages.', 'content': GOOD_SKILL}, existing)
        self.assertIn('skill-1', str(error.exception))
        doc, _ = optimize_skill({'name': 'Landing page audit', 'description': 'Use when auditing a landing page.', 'content': GOOD_SKILL},
                                existing, skill_id='skill-1')
        self.assertTrue(doc['content'].startswith('---'))


class AuthoringToolTests(unittest.TestCase):
    setUp = runtime.RuntimeTests.setUp
    tearDown = runtime.RuntimeTests.tearDown
    ready = runtime.RuntimeTests.ready

    def newrun(self, text='Tạo báo cáo giúp tôi'):
        self.ready()
        return self.engine.chat({'message': text, 'agent_id': 'a-lead', 'channel': 'dm-a-lead'})

    def call(self, name, args, run):
        return self.engine.tool(self.store.get('agents', 'a-lead'), {'name': name, 'arguments': args}, run)

    def test_internal_artifacts_must_be_english(self):
        run = self.newrun()
        with self.assertRaises(DomainError):
            self.call('memory.write', {'title': 'Ghi chú', 'content': 'Khách hàng muốn báo cáo hàng tuần'}, run)
        self.call('memory.write', {'title': 'Owner preference', 'content': 'The owner wants a weekly report titled "Báo cáo tuần".'}, run)
        with self.assertRaises(DomainError):
            self.call('team.message', {'agent_id': 'a-content', 'content': 'Viết giúp tôi bài blog về sản phẩm'}, run)
        with self.assertRaises(DomainError):
            self.call('tasks.delegate', {'agent_id': 'a-content', 'title': 'Viết bài blog', 'description': 'Viết bài'}, run)
        with self.assertRaises(DomainError):
            self.call('agents.create', {'name': 'Writer', 'role': 'Content writer', 'instructions': 'Viết nội dung hay cho khách hàng'}, run)
        with self.assertRaises(DomainError):
            self.call('agents.create', {'name': 'Writer', 'role': 'Content writer', 'instructions': 'Write well. ' * 400}, run)
        created = self.call('skills.create', {'name': 'Landing page audit', 'description': 'Use when auditing a landing page.',
                                              'content': GOOD_SKILL}, run)
        self.assertTrue(self.store.get('skills', created['id'])['content'].startswith('---\nname: landing-page-audit'))

    def test_episode_memory_and_handoff_use_english_summary(self):
        run = self.newrun()
        self.store.patch('runs', run['id'], {'summary': 'Drafted the weekly report and saved reports/week.md.'})
        title, content = self.engine.episode_memory(self.store.get('runs', run['id']), 'Em đã hoàn thành báo cáo tuần cho anh.')
        self.assertEqual(content, 'Drafted the weekly report and saved reports/week.md.')
        self.assertEqual(foreign_ratio(title), 0)
        self.store.patch('runs', run['id'], {'summary': None})
        title, content = self.engine.episode_memory(self.store.get('runs', run['id']), 'Em đã hoàn thành báo cáo tuần cho anh.')
        self.assertEqual(foreign_ratio(content), 0)
        self.assertIn('Vietnamese', content)

    def test_short_follow_up_keeps_conversation_language(self):
        first = self.newrun()
        self.assertEqual(first['owner_language'], 'vi')
        second = self.engine.chat({'message': 'ok go', 'agent_id': 'a-lead', 'channel': 'dm-a-lead'})
        self.assertEqual(second['owner_language'], 'vi')


if __name__ == '__main__':
    unittest.main()
