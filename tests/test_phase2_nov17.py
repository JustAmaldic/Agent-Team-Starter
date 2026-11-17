import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ats  # noqa: E402
from ats_modules import checkpoints, dag  # noqa: E402


def git(*args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class CheckpointReasonTests(unittest.TestCase):
    def test_every_rejection_has_its_own_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = ["First reviewed component", "Second reviewed component"]
            worktree, head = Path(tmp) / "work", "a" * 40
            self.assertEqual(checkpoints.diagnose(tmp, "run-1", plan, worktree, head), "MISSING")
            checkpoints.save_checkpoint(tmp, "run-1", plan, worktree, head, 1,
                                        [{"subtask": 1, "implementer": "codex", "reviewer": "claude"}])
            self.assertIsNone(checkpoints.diagnose(tmp, "run-1", plan, worktree, head))
            self.assertEqual(checkpoints.diagnose(tmp, "run-1", plan, worktree, "b" * 40), "HEAD_CHANGED")
            self.assertEqual(checkpoints.diagnose(tmp, "run-1", plan, Path(tmp) / "other", head), "WORKTREE_CHANGED")
            self.assertEqual(checkpoints.diagnose(tmp, "run-1", list(reversed(plan)), worktree, head), "PLAN_CHANGED")
            checkpoints.checkpoint_path(tmp, "run-1").write_text("{broken")
            self.assertEqual(checkpoints.diagnose(tmp, "run-1", plan, worktree, head), "INVALID")
            for code in checkpoints.REASONS:
                self.assertTrue(checkpoints.explain(code))

    def test_diagnosis_never_makes_a_rejected_checkpoint_resumable(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan, worktree = ["a part", "b part"], Path(tmp) / "w"
            checkpoints.save_checkpoint(tmp, "r", plan, worktree, "a" * 40, 1,
                                        [{"subtask": 1, "implementer": "codex", "reviewer": "claude"}])
            checkpoints.diagnose(tmp, "r", plan, worktree, "c" * 40)
            self.assertIsNone(checkpoints.load_checkpoint(tmp, "r", plan, worktree, "c" * 40))


class OwnedPathTests(unittest.TestCase):
    def plan(self, paths):
        return {"plan_version": 2, "tasks": [
            {"id": "a", "title": "Build the first part", "depends_on": [], "owned_paths": paths},
            {"id": "b", "title": "Build the second part", "depends_on": [], "owned_paths": ["docs/b.md"]}]}

    def test_paths_outside_the_worktree_are_refused(self):
        dag.validate_dag(self.plan(["src/app.py", "tests\\test_app.py"]))
        for bad in (["~/Desktop/x.py"], ["C:/Users/x.py"], ["..\\other\\x.py"], ["/etc/hosts"],
                    ["src/../../x.py"], ["$HOME/x"], ["%APPDATA%/x"]):
            with self.assertRaises(ValueError) as caught:
                dag.validate_dag(self.plan(bad))
            self.assertNotIn(bad[0].split('/')[-1], str(caught.exception))


class FlakyFollowUpTests(unittest.TestCase):
    def test_third_unstable_run_proposes_a_task(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ats, 'DATA', Path(tmp)), \
                mock.patch.object(ats, 'project', lambda: {'project': 'p'}), \
                mock.patch.object(ats, 'record') as record:
            events = Path(tmp) / 'p' / 'events'
            events.mkdir(parents=True)
            checks = [{'command': 'pytest -q', 'passed': True, 'flaky': True}]
            ats.note_repeated_flaky([dict(checks[0])], 't')
            self.assertFalse(record.called)
            for i in range(3):
                (events / f'{i}.json').write_text(json.dumps({'event_type': 'test_flaky', 'command': 'pytest -q'}))
            ats.note_repeated_flaky(checks, 't')
            ats.note_repeated_flaky(checks, 't')   # once per check
            self.assertEqual(record.call_count, 1)
            self.assertEqual(record.call_args.args[0], 'test_flaky_repeated')
            self.assertIn('Make the unstable test command reliable', record.call_args.kwargs['proposed_task'])


class FailingOnBaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / 'repo'
        self.repo.mkdir()
        git('init', '-q', cwd=self.repo)
        git('config', 'user.name', 't', cwd=self.repo)
        git('config', 'user.email', 't@t', cwd=self.repo)
        (self.repo / 'ok.txt').write_text('1')
        git('add', '-A', cwd=self.repo)
        git('commit', '-q', '-m', 'base', cwd=self.repo)

    def tearDown(self):
        self.temp.cleanup()

    def run_mark(self, command_fails_on_base):
        checks = [{'command': 'check', 'passed': False, 'output': 'boom'}]
        outcomes = []

        def fake(cmd, folder, timeout=0):
            outcomes.append(str(folder))
            return SimpleNamespace(returncode=1 if command_fails_on_base else 0, stdout='', stderr='')
        with mock.patch.object(ats, 'run_check', fake), mock.patch.object(ats, 'record') as record, \
                mock.patch.dict(ats._run_flags, {}, clear=True):
            existing = ats.mark_failing_on_base({}, self.repo, 'HEAD', checks, 'task')
            ats.mark_failing_on_base({}, self.repo, 'HEAD', [dict(checks[0], fails_on_base=None)], 'task')
        return checks, existing, outcomes, record

    def test_failure_that_exists_on_base_is_labelled(self):
        checks, existing, outcomes, record = self.run_mark(True)
        self.assertTrue(checks[0]['fails_on_base'])
        self.assertEqual(len(existing), 1)
        self.assertEqual(len(outcomes), 1)                                   # cached per base and command
        self.assertNotEqual(outcomes[0], str(self.repo))                     # a separate checkout
        self.assertEqual(record.call_args.args[0], 'test_fails_on_base')
        reasons = ats.evaluate_approval('APPROVE', checks, [], [], 'claude', 'codex', {'test_commands': ['check']})
        self.assertTrue(any('already fail on the base branch' in r for r in reasons))   # still not approved

    def test_failure_caused_by_the_change_is_not_labelled(self):
        checks, existing, _, record = self.run_mark(False)
        self.assertEqual((checks[0]['fails_on_base'], existing), (False, []))
        self.assertFalse(record.called)


if __name__ == '__main__':
    unittest.main()
