"""Smarter task division: ownership enforced before merging, an integration review of the combined
change, and plans that learn from earlier conflicts in the same project."""
import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats
from ats_modules import plan_learning

PASS = [sys.executable, '-c', 'pass']
APPROVE = 'ATS_FINDINGS_JSON: []\nVERDICT: APPROVE'


def git(folder, *args):
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args], cwd=folder,
                          capture_output=True, text=True, check=True).stdout


def dag(*nodes):
    return {'plan_version': 2, 'tasks': [
        {'id': node_id, 'title': title, 'depends_on': deps, 'owned_paths': owned}
        for node_id, title, deps, owned in nodes]}


class PlanLearningTests(unittest.TestCase):
    EVENTS = [
        {'event_type': 'task_split', 'task_id': 'r1'},
        {'event_type': 'merge_conflict', 'task_id': 'r1', 'subtask': 2, 'files': ['ats.py', 'CHANGELOG.md']},
        {'event_type': 'merge_conflict', 'task_id': 'r2', 'subtask': 4,
         'summary': 'Merging your part into the other part produced conflicts in: ats.py. Resolve every marker'},
        {'event_type': 'merge_conflict', 'task_id': 'r2', 'base': 'origin/main', 'files': ['VERSION']},
        {'event_type': 'ownership_drift', 'task_id': 'r2', 'paths': ['README.md']},
        {'event_type': 'integration_review', 'task_id': 'r2', 'files': ['ats_modules/report.py']},
        {'event_type': 'wave_timing', 'task_id': 'r2', 'idle_seconds': 120},
        {'event_type': 'wave_timing', 'task_id': 'r2', 'idle_seconds': 60},
    ]

    def test_history_counts_what_went_wrong_between_parts(self):
        hist = plan_learning.history(self.EVENTS)
        self.assertEqual(hist['parallel_conflicts']['ats.py'], 2)   # structured and parsed from an old summary
        self.assertEqual(hist['base_conflicts']['VERSION'], 1)
        self.assertEqual(hist['pairs'], 2)
        self.assertEqual(hist['idle_seconds'], 90)
        self.assertEqual(plan_learning.shared_files(hist), ['CHANGELOG.md', 'ats.py', 'ats_modules/report.py'])

    def test_planner_note_is_factual_and_empty_without_history(self):
        note = plan_learning.planner_note(plan_learning.history(self.EVENTS))
        self.assertIn('Merge conflicts between parallel parts in: ats.py (2x)', note)
        self.assertIn('Give each file listed above to exactly ONE node', note)
        self.assertIn('waited 90 s on average', note)
        self.assertEqual(plan_learning.planner_note(plan_learning.history([])), '')

    def test_planner_prompt_carries_the_lessons(self):
        prompts = []
        output = 'ATS_DAG_JSON: ' + json.dumps(dag(
            ('a', 'Build the first bounded module with tests', [], ['a.py']),
            ('b', 'Build the second bounded module with tests', [], ['b.py'])))
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(ats, 'enabled_agents', return_value=['codex', 'claude']), \
             patch.object(ats, 'phase'), patch.object(ats, 'index_tree', return_value='t'), \
             patch.object(ats, 'restore_if_changed', return_value=[]), patch.object(ats, 'record'), \
             patch.object(ats, 'shared_prefix', return_value=''), patch('builtins.print'), \
             patch.object(ats, 'plan_history', return_value=plan_learning.history(self.EVENTS)), \
             patch.object(ats, 'invoke_with_fallback', side_effect=lambda a, p, *x, **k: prompts.append(p) or (a, output)):
            ats.preplan_task('Build two modules ' * 10, 't1', pathlib.Path(tmp), 'codex', set(), {}, mode='always')
        self.assertIn('Lessons from earlier ATS runs in this project', prompts[0])


class ReviewLoopOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self.temp.name)
        git(self.folder, 'init', '-q', '-b', 'main')
        for name in ('api.py', 'screen.py'):
            (self.folder / name).write_text('x = 1\n')
        git(self.folder, 'add', '-A')
        git(self.folder, 'commit', '-q', '-m', 'init')
        self.events = []

    def tearDown(self):
        self.temp.cleanup()

    def test_editing_the_parallel_parts_file_blocks_until_undone(self):
        (self.folder / 'api.py').write_text('x = 2\n')
        (self.folder / 'screen.py').write_text('x = 3\n')   # owned by the parallel part
        steps = [('claude', lambda f, p: APPROVE)]

        def undo(folder, prompt):
            self.assertIn('OWN1', prompt)
            (folder / 'screen.py').write_text('x = 1\n')
            return 'ATS_RESPONSES_JSON: [{"id":"OWN1","status":"fixed","note":"reverted"}]'

        steps += [('codex', undo), ('claude', lambda f, p: APPROVE)]

        def invoke(agent, prompt, cwd, quiet=False, **kw):
            expected, action = steps.pop(0)
            self.assertEqual(agent, expected)
            return action(pathlib.Path(cwd), prompt)

        with patch.object(ats, 'invoke', side_effect=invoke), \
             patch.object(ats, 'record', side_effect=lambda kind, summary, task=None, **f:
                          self.events.append(dict(kind=kind, **f))), \
             patch.object(ats, 'knowledge_context', return_value=''), patch.object(ats, 'capture_learning_proposals'), \
             patch.object(ats, 'enabled_agents', return_value=['codex', 'claude']), \
             patch.object(ats, 'executable', return_value='x'), patch.object(ats, 'codex_preflight'):
            approved, *_ = ats.review_loop('task', 'tid', self.folder, 'ats/b', 'codex', ats.UnavailableAgents(),
                                           {'test_commands': [PASS], 'max_revisions': 1}, quiet=True,
                                           owned_paths=['api.py'], reserved_paths=['screen.py'])
        self.assertTrue(approved)
        self.assertEqual(steps, [])
        blocked = [e for e in self.events if e['kind'] == 'ownership_blocked']
        self.assertEqual(blocked[0]['paths'], ['screen.py'])

    def test_own_files_are_never_reserved(self):
        self.assertEqual(ats.reserved_changes(['api.py', 'shared/x.py'], ['shared/', 'api.py'], ['api.py']),
                         ['shared/x.py'])


class PairWithIntegrationReviewTests(unittest.TestCase):
    """A real pair in two worktrees: ownership told to both builders, then one integration review."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self.temp.name) / 'app-ats-1234'
        self.folder.mkdir()
        git(self.folder, 'init', '-q', '-b', 'main')
        (self.folder / 'app.py').write_text('base = 1\n')
        git(self.folder, 'add', '-A')
        git(self.folder, 'commit', '-q', '-m', 'init')
        git(self.folder, 'switch', '-q', '-c', 'ats/b')
        self.calls, self.events = [], []
        self.lock = threading.Lock()

    def tearDown(self):
        self.temp.cleanup()

    def test_pair_then_integration_review_of_the_combined_change(self):
        plan = dag(('api', 'Build the API module with tests', [], ['api.py']),
                   ('screen', 'Build the settings screen module with tests', [], ['screen.py']))

        def invoke(agent, prompt, cwd, quiet=False, **kw):
            cwd = pathlib.Path(cwd)
            with self.lock:
                self.calls.append((agent, cwd.name, prompt))
            if 'Implement ONLY' in prompt:
                time.sleep(0.1)
                name = 'api.py' if 'Subtask 1/2' in prompt else 'screen.py'
                (cwd / name).write_text(f'by = "{agent}"\n')
                return 'built\nATS_LEARNINGS_JSON: []'
            return APPROVE

        history = plan_learning.history([{'event_type': 'merge_conflict', 'subtask': 2, 'files': ['app.py']}])
        with patch.object(ats, 'invoke', side_effect=invoke), \
             patch.object(ats, 'record', side_effect=lambda kind, summary, task=None, **f:
                          self.events.append(dict(kind=kind, summary=summary, **f))), \
             patch.object(ats, 'phase'), patch.object(ats, 'capture_learning_proposals'), \
             patch.object(ats, 'codex_preflight'), patch.object(ats, 'knowledge_context', return_value=''), \
             patch.object(ats, 'plan_history', return_value=history), \
             patch.object(ats, 'enabled_agents', return_value=['codex', 'claude']), \
             patch.object(ats, 'executable', return_value='x'), \
             patch.object(ats, 'integration_base', return_value=git(self.folder, 'rev-parse', 'main').strip()):
            approved, *_ = ats.subtask_flow('big task', plan, 'tid', self.folder, 'ats/b', 'codex',
                                            ats.UnavailableAgents(), {'test_commands': [PASS], 'max_revisions': 0},
                                            quiet=True)
        self.assertTrue(approved)
        builders = [p for a, c, p in self.calls if 'Implement ONLY' in p]
        self.assertTrue(all('Do NOT edit these files' in p and 'app.py' in p for p in builders))
        integration = [(a, p) for a, c, p in self.calls if 'INTEGRATION REVIEW' in p]
        self.assertEqual(len(integration), 1)
        self.assertIn('+by = ', integration[0][1])          # it sees the combined diff of both parts
        summary = next(e for e in self.events if e['kind'] == 'subtasks_completed')
        self.assertTrue(summary['combined_review'])
        result = next(e for e in self.events if e['kind'] == 'integration_review')
        self.assertTrue(result['approved'])


if __name__ == '__main__':
    unittest.main()
