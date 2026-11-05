"""Style: a gate on literal colours and fonts, the project's UI vocabulary, a UI reference per part,
style findings as rules. Visibility: skipped tests, required tools, and labelled review passes."""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats
from ats_modules import style_check, test_env

CSS = """:root { --bg: #0A0A0B; --accent: #7C5CFF; }
.card { background: var(--bg); }
.btn { color: var(--accent); }
.btn.primary { border: 1px solid var(--accent); }
.field input { padding: 4px; }
#fade { opacity: 0; }
"""
JS = """function h(tag, props) { return tag; }
function field(id, label, input) { return h('label', { class: 'field' }); }
const a = h('div', { class: 'card pad' }); h('button', { class: 'btn primary' }); h('div', { class: 'card' });
h('a', { class: 'btn' }); h('b', { class: 'btn' }); field(1); field(2); field(3); field(4); field(5);
h('p'); h('p'); h('p');
"""


class StyleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        (self.root / 'web').mkdir()
        (self.root / 'web' / 'app.css').write_text(CSS)
        (self.root / 'web' / 'app.js').write_text(JS)

    def tearDown(self):
        self.temp.cleanup()

    def test_unknown_classes(self):
        unknown = style_check.unknown_classes(self.root, {'web/screen.js': ["h('div', { class: 'card fancy-box' })"]})
        self.assertEqual(unknown, ['fancy-box'])


class VisibilityTests(unittest.TestCase):
    def test_skipped_tests_are_counted(self):
        self.assertEqual(ats.skipped_tests('Ran 368 tests in 4s\n\nOK (skipped=3)'), 3)
        self.assertEqual(ats.skipped_tests('== 40 passed, 2 skipped in 1.2s =='), 2)
        self.assertEqual(ats.skipped_tests('OK'), 0)
        with patch.object(ats, 'run_check', return_value=subprocess.CompletedProcess([], 0, 'OK (skipped=3)', '')):
            checks = ats.run_checks({'test_commands': ['python -m unittest']}, pathlib.Path('.'))
        self.assertEqual(checks[0]['skipped'], 3)

    def test_javascript_tests_need_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'tests').mkdir()
            self.assertEqual(test_env.required_tools({}, root), [])
            (root / 'tests' / 'screen.cjs').write_text('')
            self.assertEqual(test_env.required_tools({}, root), ['node'])
            self.assertEqual(test_env.required_tools({'required_tools': []}, root), [])
        self.assertEqual(test_env.missing_tools(['definitely-not-a-tool-ats'], {'PATH': '/nonexistent'}, None),
                         ['definitely-not-a-tool-ats'])

    def test_missing_tool_blocks_before_agents(self):
        events = []
        with patch.object(ats.test_env, 'required_tools', return_value=['node']), \
             patch.object(ats.test_env, 'missing_tools', return_value=['node']), \
             patch.object(ats.test_env, 'check', return_value={'ok': True, 'python': None, 'env': None,
                                                               'source': None, 'missing': [], 'message': ''}), \
             patch.object(ats, 'record', side_effect=lambda kind, summary, task=None, **f: events.append(f)):
            problem = ats.environment_problem({'test_commands': ['python -m unittest']}, 'tid')
        self.assertIn('node not found', problem)
        self.assertEqual(events[-1]['missing_tools'], ['node'])


class ReviewLabelTests(unittest.TestCase):
    def test_review_result_says_which_review_it_was(self):
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            subprocess.run(['git', 'init', '-q'], cwd=folder, check=True)
            subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty',
                            '-m', 'i'], cwd=folder, check=True)
            (folder / 'a.py').write_text('x = 1\n')
            with patch.object(ats, 'invoke', return_value='ATS_FINDINGS_JSON: []\nVERDICT: APPROVE'), \
                 patch.object(ats, 'record', side_effect=lambda kind, summary, task=None, **f:
                              events.append(dict(kind=kind, **f))), \
                 patch.object(ats, 'knowledge_context', return_value=''), \
                 patch.object(ats, 'capture_learning_proposals'), patch.object(ats, 'phase'), \
                 patch.object(ats, 'enabled_agents', return_value=['codex', 'claude']), \
                 patch.object(ats, 'executable', return_value='x'), patch.object(ats, 'codex_preflight'):
                ats.review_loop('task', 'tid', folder, 'ats/b', 'codex', ats.UnavailableAgents(),
                                {'test_commands': [[sys.executable, '-c', 'pass']]}, quiet=True, kind='integration')
        result = next(e for e in events if e['kind'] == 'review_result')
        self.assertEqual(result['review_kind'], 'integration')
        self.assertEqual(result['passes'], ['functional'])


if __name__ == '__main__':
    unittest.main()
