"""The project's test environment: its virtualenv is used, missing packages stop the run before any
agent starts, and a missing package in test output is a configuration problem, never a repair."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats
from ats_modules import gate, test_env
from ats_modules.budget import StuckDetector

TRACEBACK = """E
======================================================================
ERROR: test_api (unittest.loader._FailedTest.test_api)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_api
Traceback (most recent call last):
  File "/usr/lib/python3.12/unittest/loader.py", line 396, in _find_test_path
    module = self._get_module_from_name(name)
  File "tests/test_api.py", line 3, in <module>
    from fastapi.testclient import TestClient
ModuleNotFoundError: No module named 'fastapi'

----------------------------------------------------------------------
Ran 1 test in 0.000s

FAILED (errors=1)
"""


def make_venv(root):
    python = test_env.python_in(root / '.venv')
    python.parent.mkdir(parents=True)
    python.write_text('')
    return root / '.venv'


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_auto_detects_dot_venv_and_puts_it_first_in_path(self):
        env = make_venv(self.root)
        resolved = test_env.resolve({}, self.root)
        self.assertEqual((resolved['env'], resolved['source']), (env, 'auto'))
        result = test_env.environ({'PATH': '/usr/bin', 'PYTHONHOME': 'x'}, env)
        self.assertTrue(result['PATH'].startswith(str(test_env.scripts_dir(env)) + os.pathsep))
        self.assertEqual(result['VIRTUAL_ENV'], str(env))
        self.assertNotIn('PYTHONHOME', result)

    def test_configured_env_must_exist(self):
        self.assertIsNotNone(test_env.resolve({'python_env': 'envs/missing'}, self.root)['error'])
        env = self.root / 'envs' / 'tests'
        test_env.python_in(env).parent.mkdir(parents=True)
        test_env.python_in(env).write_text('')
        self.assertEqual(test_env.resolve({'python_env': 'envs/tests'}, self.root)['env'], env)

    def test_no_env_changes_nothing(self):
        self.assertIsNone(test_env.resolve({}, self.root)['env'])
        self.assertEqual(test_env.environ({'PATH': '/usr/bin'}, None), {'PATH': '/usr/bin'})

    def test_requirements_skip_markers_urls_and_options(self):
        (self.root / 'requirements.txt').write_text(
            'fastapi>=0.110\nuvicorn[standard]==0.30  # server\n-r other.txt\n'
            'pywin32; sys_platform == "win32"\ngit+https://x/y.git\n\n')
        (self.root / 'requirements-dev.txt').write_text('pytest\nfastapi\n')
        self.assertEqual(test_env.requirements(self.root), ['fastapi', 'uvicorn', 'pytest'])

    def test_python_of_test_commands(self):
        self.assertEqual(test_env.python_of('python -m unittest discover -s tests'), 'python')
        self.assertEqual(test_env.python_of(['python3.12', '-m', 'pytest']), 'python3.12')
        self.assertIsNone(test_env.python_of('npm test'))
        self.assertIsNone(test_env.python_of('pytest -q'))


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        (self.root / 'requirements.txt').write_text('fastapi\nnot-a-real-package-ats\n')

    def tearDown(self):
        self.temp.cleanup()

    def test_reports_missing_packages_of_the_real_test_python(self):
        report = test_env.check({'test_commands': [f'{sys.executable} -m unittest']}, self.root, dict(os.environ))
        self.assertFalse(report['ok'])
        self.assertIn('not-a-real-package-ats', report['missing'])
        self.assertIn('Agents never install packages', report['message'])

    def test_ok_when_everything_is_installed(self):
        (self.root / 'requirements.txt').write_text('pip\n')
        report = test_env.check({'test_commands': [f'{sys.executable} -m unittest']}, self.root, dict(os.environ))
        self.assertTrue(report['ok'], report)
        self.assertIsNotNone(report['python'])

    def test_non_python_commands_are_not_checked(self):
        calls = []
        report = test_env.check({'test_commands': ['npm test']}, self.root, {}, run=lambda *a, **k: calls.append(a))
        self.assertTrue(report['ok'])
        self.assertEqual(calls, [])

    def test_the_virtualenv_python_is_the_one_checked(self):
        env = make_venv(self.root)
        seen = {}

        def run(argv, **kwargs):
            seen['path'] = kwargs['env']['PATH']
            return subprocess.CompletedProcess(argv, 0, json.dumps({'python': 'venv-python', 'missing': []}), '')
        report = test_env.check({'test_commands': ['python -m unittest']}, self.root, {'PATH': '/usr/bin'}, run=run)
        self.assertTrue(report['ok'])
        self.assertEqual(report['source'], 'auto')
        self.assertTrue(seen['path'].startswith(str(test_env.scripts_dir(env))))


class MissingModuleTests(unittest.TestCase):
    def test_third_party_module_in_a_long_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(test_env.missing_module(TRACEBACK, pathlib.Path(tmp)), 'fastapi')
            self.assertTrue(ats.cannot_start(1, TRACEBACK, pathlib.Path(tmp)))
            self.assertFalse(ats.cannot_start(1, TRACEBACK))   # without a folder: old behaviour

    def test_a_project_module_is_a_code_problem(self):
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / 'fastapi').mkdir()
            self.assertIsNone(test_env.missing_module(TRACEBACK, pathlib.Path(tmp)))
            (pathlib.Path(tmp) / 'src').mkdir()
            (pathlib.Path(tmp) / 'src' / 'helpers.py').write_text('')
            self.assertIsNone(test_env.missing_module("ModuleNotFoundError: No module named 'helpers'", tmp))

    def test_gate_marks_it_unavailable_not_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome = gate._run('targeted tests', gate.TARGETED, ['python', '-m', 'unittest'], pathlib.Path(tmp),
                                lambda *a, **k: subprocess.CompletedProcess(a[0], 1, TRACEBACK, ''), 60)
        self.assertEqual(outcome['status'], gate.UNAVAILABLE)
        self.assertEqual(outcome['environment'], 'fastapi')


class GateStopsWithoutRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self.temp.name)
        subprocess.run(['git', 'init', '-q'], cwd=self.folder, check=True)
        (self.folder / 'app.py').write_text('x = 1\n')

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_package_stops_the_gate_at_once(self):
        results = [{'name': 'targeted tests', 'level': 'targeted', 'status': 'unavailable', 'detail': TRACEBACK,
                    'environment': 'fastapi'}]
        budget = unittest.mock.MagicMock()
        with patch.object(ats.pre_gate, 'run_gate', return_value=results), patch.object(ats, 'record'), \
             patch.object(ats, 'phase'), patch.object(ats, 'invoke_with_fallback') as invoke:
            _, _, reason = ats.pre_review_gate('task', 'tid', self.folder, '4b825dc642cb6eb9a060e54bf8d69288fbee4904',
                                               'codex', ats.UnavailableAgents(), {}, budget, StuckDetector(), {})
        invoke.assert_not_called()
        budget.take_repair.assert_not_called()
        self.assertIn('test environment incomplete: fastapi', reason)

    def test_stuck_message_keeps_the_real_cause(self):
        results = [{'name': 'ruff', 'level': 'fast', 'status': 'failed', 'detail': 'E999 config error'}]
        budget = unittest.mock.MagicMock()
        budget.exceeded.return_value = None
        budget.take_repair.return_value = True
        with patch.object(ats.pre_gate, 'run_gate', return_value=results), patch.object(ats, 'record'), \
             patch.object(ats, 'phase'), \
             patch.object(ats, 'invoke_with_fallback', return_value=('codex', 'This is a configuration problem.')):
            _, _, reason = ats.pre_review_gate('task', 'tid', self.folder, '4b825dc642cb6eb9a060e54bf8d69288fbee4904',
                                               'codex', ats.UnavailableAgents(), {}, budget, StuckDetector(), {})
        self.assertIn('changed no files', reason)
        self.assertIn('E999 config error', reason)
        self.assertIn('codex said: This is a configuration problem.', reason)


class RunChecksTests(unittest.TestCase):
    def test_missing_package_blocks_with_a_clear_reason(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(ats, 'run_check', return_value=subprocess.CompletedProcess([], 1, TRACEBACK, '')):
            checks = ats.run_checks({'test_commands': ['python -m unittest']}, pathlib.Path(tmp))
        self.assertTrue(checks[0]['cannot_start'])
        self.assertEqual(checks[0]['missing_module'], 'fastapi')


class TaskFlowEnvironmentTests(unittest.TestCase):
    def test_no_agent_starts_when_the_environment_is_incomplete(self):
        events = []
        with patch.object(ats, 'project', return_value={'project': 'p', 'test_commands': ['python -m unittest']}), \
             patch.object(ats, 'execute', return_value=subprocess.CompletedProcess([], 0, str(ats.APP), '')), \
             patch.object(ats, 'enabled_agents', return_value=['codex', 'claude']), \
             patch.object(ats, 'select', return_value='codex'), \
             patch.object(ats, 'record', side_effect=lambda kind, summary, task=None, **f: events.append(kind)), \
             patch.object(ats, 'phase'), patch('builtins.print'), \
             patch.object(ats.test_env, 'check', return_value={
                 'ok': False, 'python': '/usr/bin/python3', 'env': None, 'source': None,
                 'missing': ['fastapi'], 'message': 'Test environment incomplete: fastapi'}), \
             patch.object(ats, 'invoke_with_fallback') as invoke, patch.object(ats, 'preplan_task') as plan:
            outcome = ats._task_flow('Add a dropdown', 'auto', False, 'tid')
        self.assertEqual(outcome, 'blocked')
        invoke.assert_not_called()
        plan.assert_not_called()
        self.assertEqual(events[-2:], ['test_environment', 'blocked'])


class RecheckTests(unittest.TestCase):
    """The kept worktree of a run that stopped on its environment is tested and reviewed again."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.temp.name)
        self.app = base / 'app'
        self.app.mkdir()
        git = ['git', '-c', 'user.name=t', '-c', 'user.email=t@t']
        subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=self.app, check=True)
        (self.app / 'app.py').write_text('x = 1\n')
        subprocess.run(git + ['add', '-A'], cwd=self.app, check=True)
        subprocess.run(git + ['commit', '-q', '-m', 'init'], cwd=self.app, check=True)
        self.source = 'e7252e2f-1869-48ad-b133-7dac68a42492'
        self.folder = base / 'app-ats-e7252e2f'
        subprocess.run(['git', 'worktree', 'add', '-q', '-b', 'ats/20261104-e7252e2f', str(self.folder)],
                       cwd=self.app, check=True)
        (self.folder / 'app.py').write_text('x = 2\n')   # the kept, unreviewed work

    def tearDown(self):
        self.temp.cleanup()

    def test_recheck_reviews_the_kept_work_without_rebuilding(self):
        calls = []
        events = [{'event_type': 'implementation', 'task_id': self.source, 'implementer': 'codex'}]
        with patch.object(ats, 'APP', self.app), \
             patch.object(ats, 'project', return_value={'project': 'app', 'test_commands': []}), \
             patch.object(ats.intelligence, 'read_events', return_value=events), \
             patch.object(ats, 'enabled_agents', return_value=['codex', 'claude']), \
             patch.object(ats, 'executable', return_value='x'), \
             patch.object(ats, 'record', side_effect=lambda kind, summary, task=None, **f: calls.append(kind)), \
             patch.object(ats, 'phase'), patch('builtins.print'), \
             patch.object(ats, 'integration_base', return_value='main'), \
             patch.object(ats, 'review_loop', return_value=(False, 'claude', 'codex', [])) as review, \
             patch.object(ats, 'distill_learnings'), patch.object(ats, 'invoke_with_fallback') as invoke, \
             patch.object(ats, 'preplan_task') as plan:
            outcome = ats._task_flow('Add a dropdown', 'auto', False, 'newid', recheck_from=self.source)
        self.assertEqual(outcome, 'escalated')
        invoke.assert_not_called()
        plan.assert_not_called()
        args, kwargs = review.call_args
        self.assertEqual(args[2], self.folder.resolve())
        self.assertEqual(args[4], 'codex')        # the builder stays implementer; claude reviews
        self.assertEqual(kwargs['base'], 'main')
        self.assertIn('rechecked', calls)
        self.assertEqual((self.folder / 'app.py').read_text(), 'x = 2\n')

    def test_recheck_refuses_a_folder_that_is_not_the_kept_worktree(self):
        with patch.object(ats, 'APP', self.app):
            with self.assertRaises(RuntimeError):
                ats.recheck_source('00000000-0000-4000-8000-000000000000', 'codex')
            with self.assertRaises(RuntimeError):
                ats.recheck_source('not-a-uuid', 'codex')


if __name__ == '__main__':
    unittest.main()
