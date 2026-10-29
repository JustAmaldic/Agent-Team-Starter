"""Tasks start from the latest origin/<base>, and the branch is updated with it before publishing."""
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats

PASS = [sys.executable, '-c', 'pass']


def git(*args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class FreshBaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.temp.name)
        self.origin, self.other, self.app = base / 'origin.git', base / 'other', base / 'app'
        git('init', '-q', '--bare', '-b', 'main', str(self.origin), cwd=base)
        git('clone', '-q', str(self.origin), str(self.other), cwd=base)
        for folder in (self.other,):
            git('config', 'user.name', 't', cwd=folder)
            git('config', 'user.email', 't@t', cwd=folder)
        (self.other / 'app.py').write_text('a = 1\nb = 1\n')
        git('add', '-A', cwd=self.other)
        git('commit', '-q', '-m', 'init', cwd=self.other)
        git('push', '-q', 'origin', 'main', cwd=self.other)
        git('clone', '-q', str(self.origin), str(self.app), cwd=base)
        git('config', 'user.name', 't', cwd=self.app)
        git('config', 'user.email', 't@t', cwd=self.app)
        self.events = []
        self.patches = [patch.object(ats, 'APP', self.app),
                        patch.object(ats, 'record', side_effect=lambda kind, *a, **f: self.events.append(kind)),
                        patch.object(ats, 'phase')]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    def upstream(self, text, message='upstream'):
        (self.other / 'app.py').write_text(text)
        git('commit', '-qam', message, cwd=self.other)
        git('push', '-q', 'origin', 'main', cwd=self.other)

    def worktree(self):
        folder = self.app.parent / 'app-ats-1'
        git('worktree', 'add', '-q', '-b', 'ats/x', str(folder), ats.fresh_base({}), cwd=self.app)
        return folder

    def test_new_task_starts_from_origin_even_when_local_main_is_behind(self):
        self.upstream('a = 2\nb = 1\n')          # GitHub moved on, the local checkout did not pull
        self.assertEqual(ats.fresh_base({}), 'origin/main')
        folder = self.worktree()
        self.assertEqual((folder / 'app.py').read_text(), 'a = 2\nb = 1\n')

    def test_no_remote_falls_back_to_head(self):
        git('remote', 'remove', 'origin', cwd=self.app)
        self.assertEqual(ats.fresh_base({}), 'HEAD')

    def commit_task(self, folder, text):
        (folder / 'app.py').write_text(text)
        git('commit', '-qam', 'task', cwd=folder)

    def test_up_to_date_branch_needs_nothing(self):
        folder = self.worktree()
        self.commit_task(folder, 'a = 1\nb = 9\n')
        self.assertEqual(ats.update_from_base('t', 'tid', folder, 'ats/x', {'test_commands': [PASS]}, 'codex',
                                              ats.UnavailableAgents()), (True, []))

    def test_clean_merge_with_passing_checks_is_done_without_agents(self):
        folder = self.worktree()
        self.commit_task(folder, 'a = 1\nb = 9\n')
        (self.other / 'new.py').write_text('n = 1\n')
        git('add', '-A', cwd=self.other)
        git('commit', '-qm', 'new file', cwd=self.other)
        git('push', '-q', 'origin', 'main', cwd=self.other)
        with patch.object(ats, 'invoke_with_fallback') as invoke:
            ok, cycles = ats.update_from_base('t', 'tid', folder, 'ats/x', {'test_commands': [PASS]}, 'codex',
                                              ats.UnavailableAgents())
        self.assertTrue(ok)
        invoke.assert_not_called()
        self.assertTrue((folder / 'new.py').exists())
        self.assertIn('base_merged', self.events)

    def test_conflict_is_resolved_by_the_implementer_and_reviewed(self):
        folder = self.worktree()
        self.commit_task(folder, 'a = 1\nb = 9\n')
        self.upstream('a = 1\nb = 5\n')

        def resolve(agent, prompt, cwd, unavailable, task_id, purpose, quiet=False, fallback=True):
            self.assertIn('conflicts in: app.py', prompt)
            (pathlib.Path(cwd) / 'app.py').write_text('a = 1\nb = 9  # rebased on 5\n')
            return agent, 'resolved'

        with patch.object(ats, 'invoke_with_fallback', side_effect=resolve), \
             patch.object(ats, 'review_loop', return_value=(True, 'claude', 'codex', [{'cycle': 0}])) as review:
            ok, cycles = ats.update_from_base('t', 'tid', folder, 'ats/x', {'test_commands': [PASS]}, 'codex',
                                              ats.UnavailableAgents())
        self.assertTrue(ok)
        self.assertEqual(review.call_args.kwargs['base'], 'origin/main')   # the reviewer sees the whole PR diff
        self.assertEqual(cycles[0]['update'], 'origin/main')
        self.assertEqual(len(git('log', '-1', '--format=%P', cwd=folder).split()), 2)   # a real merge commit
        self.assertNotIn('<<<<<<<', (folder / 'app.py').read_text())
        git('merge-base', '--is-ancestor', 'origin/main', 'HEAD', cwd=folder)   # PR will merge cleanly

    def test_rejected_resolution_blocks_publishing(self):
        folder = self.worktree()
        self.commit_task(folder, 'a = 1\nb = 9\n')
        self.upstream('a = 1\nb = 5\n')
        with patch.object(ats, 'invoke_with_fallback', return_value=('codex', 'tried')), \
             patch.object(ats, 'review_loop', return_value=(False, 'claude', 'codex', [])):
            ok, _ = ats.update_from_base('t', 'tid', folder, 'ats/x', {'test_commands': [PASS]}, 'codex',
                                         ats.UnavailableAgents())
        self.assertFalse(ok)


if __name__ == '__main__':
    unittest.main()
