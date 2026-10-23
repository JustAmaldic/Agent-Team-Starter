"""Parallel parts: both agents build a pair at the same time, review each other's part, ATS merges."""
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

PASS = [sys.executable, '-c', 'pass']
APPROVE = 'ATS_FINDINGS_JSON: []\nVERDICT: APPROVE'
REJECT = ('ATS_FINDINGS_JSON: ' + json.dumps([{'id': 'F1', 'severity': 'MAJOR', 'file': 'x', 'line': '1',
                                                'summary': 'wrong'}]) + '\nVERDICT: CHANGES_REQUESTED')


class ParallelPartsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temp.name)
        self.folder = root / 'app-ats-1234'
        self.folder.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 't')
        self.git('config', 'user.email', 't@t')
        (self.folder / 'app.py').write_text('base = 1\n')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'init')
        self.git('switch', '-q', '-c', 'ats/b')
        self.events, self.calls = [], []
        self.lock = threading.Lock()
        self.running = 0
        self.overlap = False

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, cwd=None):
        return subprocess.run(['git', *args], cwd=cwd or self.folder, capture_output=True, text=True,
                              check=True).stdout

    def run_flow(self, plan, implement, review=lambda agent, prompt: APPROVE, resolve=None):
        def invoke(agent, prompt, cwd, quiet=False):
            cwd = pathlib.Path(cwd)
            with self.lock:
                self.calls.append((agent, cwd.name, prompt))
                self.running += 1
                if self.running > 1:
                    self.overlap = True
            try:
                if 'Implement ONLY' in prompt:
                    time.sleep(0.2)   # long enough for the other agent to start too
                    implement(agent, cwd, prompt)
                    return 'built\nATS_LEARNINGS_JSON: []'
                if 'produced conflicts' in prompt or 'fail these checks' in prompt:
                    resolve(agent, cwd, prompt)
                    return 'resolved'
                return review(agent, prompt)
            finally:
                with self.lock:
                    self.running -= 1

        with patch.object(ats, 'invoke', side_effect=invoke), \
             patch.object(ats, 'record', side_effect=lambda kind, summary, task=None, **f:
                          self.events.append(dict(kind=kind, **f))), \
             patch.object(ats, 'phase'), patch.object(ats, 'capture_learning_proposals'), \
             patch.object(ats, 'codex_preflight'), patch.object(ats, 'knowledge_context', return_value=''):
            return ats.subtask_flow('big task', plan, 'tid', self.folder, 'ats/b', 'codex',
                                    ats.UnavailableAgents(), {'test_commands': [PASS], 'max_revisions': 0},
                                    quiet=True)

    def kinds(self):
        return [e['kind'] for e in self.events]

    def test_plan_waves(self):
        self.assertEqual(ats.plan_waves(['a', ['b', 'c'], 'd']), [['a'], ['b', 'c'], ['d']])
        self.assertEqual(ats.plan_waves([['a', 'b']]), [['a', 'b']])
        for bad in ([['a']], [['a', 'b', 'c']], ['a', []], ['a', ['b', 5]], [['x'] * 2] * 11, 'ab', ['only']):
            with self.subTest(plan=bad):
                self.assertIsNone(ats.plan_waves(bad))

    def test_pair_is_built_in_parallel_cross_reviewed_and_merged(self):
        def implement(agent, cwd, prompt):
            (cwd / ('api.py' if 'Subtask 1/2' in prompt else 'screen.py')).write_text(f'by = "{agent}"\n')

        approved, *_ , history = self.run_flow([['API', 'screen']], implement)
        self.assertTrue(approved)
        self.assertTrue(self.overlap, 'the two agents never worked at the same time')
        builders = {(a, c) for a, c, p in self.calls if 'Implement ONLY' in p}
        self.assertEqual(builders, {('codex', 'app-ats-1234'), ('claude', 'app-ats-1234-part2')})
        reviews = {(a, c) for a, c, p in self.calls if 'Implement ONLY' not in p}
        self.assertEqual(reviews, {('claude', 'app-ats-1234'), ('codex', 'app-ats-1234-part2')})
        self.assertIn('another agent builds part 2', [p for a, c, p in self.calls if a == 'codex'][0].lower())
        self.assertEqual((self.folder / 'api.py').read_text(), 'by = "codex"\n')
        self.assertEqual((self.folder / 'screen.py').read_text(), 'by = "claude"\n')
        self.assertFalse((self.folder.parent / 'app-ats-1234-part2').exists())   # side worktree cleaned up
        self.assertNotIn('ats/b-part2', self.git('branch'))
        self.assertIn('subtask_merged', self.kinds())
        summary = next(e for e in self.events if e['kind'] == 'subtasks_completed')
        self.assertEqual([(p['subtask'], p['implementer'], p['reviewer']) for p in summary['parts']],
                         [(1, 'codex', 'claude'), (2, 'claude', 'codex')])

    def test_conflict_is_resolved_by_the_second_author_and_reviewed_by_the_first(self):
        def implement(agent, cwd, prompt):
            (cwd / 'app.py').write_text(f'base = 1\nchanged_by = "{agent}"\n')

        def resolve(agent, cwd, prompt):
            self.assertEqual(agent, 'claude')
            self.assertIn('app.py', prompt)
            (cwd / 'app.py').write_text('base = 1\nchanged_by = ["codex", "claude"]\n')

        approved, *_ = self.run_flow([['one', 'two']], implement, resolve=resolve)
        self.assertTrue(approved)
        self.assertIn('merge_conflict', self.kinds())
        resolution_review = [(a, p) for a, c, p in self.calls if 'Integration of part 2' in p]
        self.assertEqual(resolution_review[0][0], 'codex')
        self.assertEqual((self.folder / 'app.py').read_text(), 'base = 1\nchanged_by = ["codex", "claude"]\n')
        parents = self.git('log', '-1', '--format=%P').split()
        self.assertEqual(len(parents), 2)   # a real merge commit
        self.assertNotIn('<<<<<<<', (self.folder / 'app.py').read_text())

    def test_rejected_part_stops_and_keeps_the_side_worktree(self):
        def implement(agent, cwd, prompt):
            (cwd / f'{agent}.py').write_text('x = 1\n')

        approved, *_ = self.run_flow([['one', 'two'], 'three'], implement,
                                     review=lambda agent, prompt: REJECT if agent == 'codex' else APPROVE)
        self.assertFalse(approved)
        self.assertTrue((self.folder.parent / 'app-ats-1234-part2').exists())
        self.assertFalse(any('Subtask 3/3' in p for a, c, p in self.calls))   # later parts never start

    def test_sequential_part_after_a_pair_starts_with_the_other_agent(self):
        def implement(agent, cwd, prompt):
            name = {'Subtask 1/3': 'a.py', 'Subtask 2/3': 'b.py', 'Subtask 3/3': 'c.py'}
            for key, file in name.items():
                if key in prompt:
                    (cwd / file).write_text(f'by = "{agent}"\n')

        approved, *_ = self.run_flow([['one', 'two'], 'three'], implement)
        self.assertTrue(approved)
        third = [a for a, c, p in self.calls if 'Implement ONLY' in p and 'Subtask 3/3' in p]
        self.assertEqual(third, ['claude'])
        for file in ('a.py', 'b.py', 'c.py'):
            self.assertTrue((self.folder / file).exists(), file)


if __name__ == '__main__':
    unittest.main()
