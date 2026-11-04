"""Tests and agents run isolated from the live run; Claude may run tests; faster reviews."""
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats
from ats_modules import gate, usage


class IsolationTests(unittest.TestCase):
    def test_test_commands_never_see_ats_control_variables(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.dict(os.environ, {'ATS_EVENTS_FILE': '/live/events.jsonl', 'ATS_TASK_ID': 'live-run',
                                     'ATS_DATA_REPO': '/home/me/.ats-data', 'ATS_MODEL_CLAUDE': 'x'}):
            code = ('import os, json; print(json.dumps({k: v for k, v in os.environ.items() '
                    'if k.startswith("ATS_")}))')
            out = ats.run_check([sys.executable, '-c', code], folder)
        import json
        seen = json.loads(out.stdout)
        self.assertNotIn('ATS_EVENTS_FILE', seen)
        self.assertNotIn('ATS_TASK_ID', seen)
        self.assertNotIn('ATS_MODEL_CLAUDE', seen)
        self.assertEqual(seen['ATS_DATA_REPO'], 'none')
        self.assertTrue(pathlib.Path(seen['ATS_STATE_DIR']).is_dir())

    def test_agent_clis_get_the_isolated_environment(self):
        with patch.dict(os.environ, {'ATS_EVENTS_FILE': '/live/events.jsonl'}):
            result = ats.stream_command([sys.executable, '-c', 'import os; print(os.environ.get("ATS_EVENTS_FILE"))'],
                                        '.', quiet=True)
        self.assertEqual(result.stdout.strip(), 'None')

    def test_data_repo_none_means_no_private_data(self):
        with patch.dict(os.environ, {'ATS_DATA_REPO': 'none'}):
            self.assertIsNone(ats.private_data_repo())


class ClaudeMayRunTestsTests(unittest.TestCase):
    def test_project_test_commands_and_read_only_git_are_allowed(self):
        allowed = ats.claude_allowed_tools({'test_commands': ['python3 -m unittest discover -s tests',
                                                              ['npm', 'test']]})
        self.assertIn('Bash(python3 -m unittest:*)', allowed)
        self.assertIn('Bash(npm:*)', allowed)
        self.assertIn('Bash(git diff:*)', allowed)
        self.assertFalse(any('git push' in a or 'git commit' in a for a in allowed))

    def test_claude_command_carries_them(self):
        with patch.object(ats, 'project', return_value={'test_commands': ['python3 -m pytest -q']}):
            command = ats.claude_print_command()
        self.assertIn('Bash(python3 -m pytest:*)', command[command.index('--allowedTools') + 1:])


class SessionGrowthTests(unittest.TestCase):
    def test_growth_adds_up_over_turns_but_ignores_cache_reads(self):
        sessions = usage.Sessions()
        for _ in range(3):
            sessions.remember('k', 'S', 'tree', {'input_tokens': 100, 'output_tokens': 9000,
                                                 'cache_read_tokens': 2_000_000})
        self.assertEqual(sessions.usable('k', 'tree'), ('S', None))


class ReviewSpeedTests(unittest.TestCase):
    def test_prefix_summarises_policy_and_reviewer_does_not_rerun_the_suite(self):
        with patch.object(ats, 'task_knowledge', return_value=''), patch.object(ats, 'repo_context', return_value=''):
            prefix = ats.shared_prefix(pathlib.Path('.'), {}, 't')
        self.assertIn('never change the computer outside the worktree', prefix)
        self.assertIn('only when you need a detail', prefix)
        self.assertIn('Do not rerun the whole suite', ats.REVIEW_ROLE)


class UnittestTargetedTests(unittest.TestCase):
    def test_one_narrowed_discover_per_related_file(self):
        conf = {'test_commands': ['python3 -m unittest discover -s tests -v']}
        commands = gate.unittest_commands(conf, ['tests/test_a.py', 'tests/test_b.py', 'other/test_c.py'])
        self.assertEqual(commands, [['python3', '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_a.py', '-v'],
                                    ['python3', '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_b.py', '-v']])
        self.assertEqual(gate.unittest_commands({'test_commands': ['make test']}, ['tests/test_a.py']), [])

    def test_gate_runs_them(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = pathlib.Path(temp)
            (folder / 'tests').mkdir()
            (folder / 'calc.py').write_text('def add(a, b):\n    return a + b\n')
            (folder / 'tests' / 'test_calc.py').write_text(
                'import sys, unittest\nsys.path.insert(0, ".")\nfrom calc import add\n\n'
                'class T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n')
            conf = {'test_commands': [f'{sys.executable} -m unittest discover -s tests']}
            outcome = gate.targeted_tests(folder, ['calc.py'], conf, ['calc.py', 'tests/test_calc.py'], ats.run_check, 60)
            self.assertEqual(outcome['status'], gate.PASSED, outcome['detail'])
            (folder / 'calc.py').write_text('def add(a, b):\n    return a - b - 0\n')   # other size: no stale .pyc
            outcome = gate.targeted_tests(folder, ['calc.py'], conf, ['calc.py', 'tests/test_calc.py'], ats.run_check, 60)
            self.assertEqual(outcome['status'], gate.FAILED)


if __name__ == '__main__':
    unittest.main()
