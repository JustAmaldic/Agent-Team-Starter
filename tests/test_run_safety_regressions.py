"""Regressions from a real ARC/ATS-app run on 2026-11-03."""
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats
from ats_contract import valid_phase

SOURCE = (pathlib.Path(__file__).resolve().parents[1] / 'ats.py').read_text(encoding='utf-8')


class RunSafetyRegressionTests(unittest.TestCase):
    def test_every_phase_ats_emits_is_in_the_contract(self):
        # phase('update') was missing: an approved --push run crashed before publishing.
        used = set(re.findall(r"phase\('([a-z_]+)'", SOURCE))
        self.assertIn('update', used)
        self.assertEqual({p for p in used if not valid_phase(p)}, set())

    def test_claude_may_read_policy_outside_the_worktree(self):
        command = ats.claude_print_command()
        self.assertEqual(command[command.index('--add-dir') + 1], str(ats.ATS))

    def test_test_command_that_cannot_start_stops_without_an_agent_repair(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = pathlib.Path(temp)
            git = ['git', '-c', 'user.name=t', '-c', 'user.email=t@t']
            subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=folder, check=True)
            (folder / 'app.py').write_text('x = 1\n')
            subprocess.run(['git', 'add', '-A'], cwd=folder, check=True)
            subprocess.run(git + ['commit', '-q', '-m', 'init'], cwd=folder, check=True)
            (folder / 'app.py').write_text('x = 2\n')
            events = []
            with patch.object(ats, 'invoke', side_effect=AssertionError('no agent call')), \
                 patch.object(ats, 'record', side_effect=lambda kind, summary, task=None, **f: events.append((kind, summary))), \
                 patch.object(ats, 'knowledge_context', return_value=''), patch('sys.stderr'):
                approved, *_ = ats.review_loop('task', 'tid', folder, 'ats/b', 'codex', ats.UnavailableAgents(),
                                               {'test_commands': ['definitely-not-python-xyz -m unittest']},
                                               quiet=True)
        self.assertFalse(approved)
        reason = next(summary for kind, summary in events if kind == 'escalated')
        self.assertIn('test command cannot start', reason)
        self.assertIn('.ats-project.json', reason)

    def test_failing_tests_are_not_mistaken_for_a_missing_program(self):
        output = 'FAIL: test_x (tests.test_app.Tests)\nAssertionError: 1 != 2\n' + 'line\n' * 20
        self.assertFalse(ats.cannot_start(1, output))
        self.assertTrue(ats.cannot_start(127, ''))
        self.assertTrue(ats.cannot_start(1, 'zsh: command not found: python\n'))


if __name__ == '__main__':
    unittest.main()
