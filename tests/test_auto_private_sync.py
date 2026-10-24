import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats


def proc(cmd, output=''):
    return subprocess.CompletedProcess(cmd, 0, output, '')


class AutoPrivateSyncTests(unittest.TestCase):
    def test_auto_sync_attempts_private_pr(self):
        with patch.object(ats, 'PRIVATE_DATA_REPO', pathlib.Path('/tmp/ats-data')), \
             patch.object(ats, 'sync_private_data') as sync:
            ats.auto_sync_private_data()
            sync.assert_called_once_with()

    def test_auto_sync_error_preserves_task_result(self):
        with patch.object(ats, 'PRIVATE_DATA_REPO', pathlib.Path('/tmp/ats-data')), \
             patch.object(ats, 'sync_private_data', side_effect=RuntimeError('offline')):
            ats.auto_sync_private_data()

    def test_without_clone_does_not_attempt_network(self):
        with patch.object(ats, 'PRIVATE_DATA_REPO', None), \
             patch.object(ats, 'sync_private_data') as sync:
            ats.auto_sync_private_data()
            sync.assert_not_called()

    def test_task_command_auto_syncs_in_finally_even_on_failure(self):
        with patch.object(sys, 'argv', ['ats.py', 'task', 'fix thing']), \
             patch.object(ats, 'sync_in'), \
             patch.object(ats, 'refresh_private_knowledge') as refresh, \
             patch.object(ats, 'task_flow', side_effect=RuntimeError('agent unavailable')), \
             patch.object(ats, 'auto_sync_private_data') as sync, \
             patch.object(ats, 'sync_out'):
            with self.assertRaisesRegex(RuntimeError, 'agent unavailable'):
                ats.main()
            refresh.assert_called_once_with()
            sync.assert_called_once_with()



class DataRepoLockTests(unittest.TestCase):
    def test_lock_serializes_and_clears_stale_locks(self):
        import os
        import time
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            (root / '.git').mkdir()
            index_lock = root / '.git' / 'index.lock'
            index_lock.write_text('')
            old = time.time() - ats.DATA_LOCK_STALE - 60
            os.utime(index_lock, (old, old))
            with ats.data_repo_lock(root):
                self.assertTrue((root / '.git' / 'ats-sync.lock').exists())
                self.assertFalse(index_lock.exists())   # stale git lock removed
                with patch.object(ats, 'DATA_LOCK_WAIT', 0), \
                     self.assertRaisesRegex(RuntimeError, 'busy'):
                    with ats.data_repo_lock(root):
                        pass
            self.assertFalse((root / '.git' / 'ats-sync.lock').exists())
            fresh = root / '.git' / 'index.lock'
            fresh.write_text('')
            with ats.data_repo_lock(root):
                self.assertTrue(fresh.exists())   # a recent git lock is left alone


class RollingDataSyncTests(unittest.TestCase):
    """Real git: a bare 'GitHub' repo, a local ~/.ats-data clone and a fake gh."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.temp.name)
        self.origin, self.local, self.other = base / 'origin.git', base / 'ats-data', base / 'merger'
        self.git('init', '-q', '--bare', '-b', 'main', str(self.origin), cwd=base)
        self.git('clone', '-q', str(self.origin), str(self.other), cwd=base)
        self.identity(self.other)
        (self.other / 'knowledge').mkdir()
        (self.other / 'knowledge' / 'decided.json').write_text('{"status": "proposed"}\n')
        self.git('add', '-A', cwd=self.other)
        self.git('commit', '-q', '-m', 'init', cwd=self.other)
        self.git('push', '-q', 'origin', 'main', cwd=self.other)
        self.git('clone', '-q', str(self.origin), str(self.local), cwd=base)
        self.identity(self.local)
        self.prs = []
        real = ats.execute

        def execute(cmd, cwd=None, **kwargs):
            if cmd[0] == 'gh':
                if cmd[1:3] == ['pr', 'list']:
                    return proc(cmd, self.prs[-1] + '\n' if self.prs else '')
                if cmd[1:3] == ['pr', 'create']:
                    self.prs.append('https://github.com/me/ATS-data/pull/%d' % (len(self.prs) + 1))
                    return proc(cmd, self.prs[-1] + '\n')
            return real(cmd, cwd, **kwargs)

        self.patches = [patch.object(ats, 'PRIVATE_DATA_REPO', self.local),
                        patch.object(ats, 'validate_private_remote', return_value='gh'),
                        patch.object(ats, 'data_remote_repo', return_value='me/ATS-data'),
                        patch.object(ats, 'data_sync_branch', return_value='ats-data/sync-pc'),
                        patch.object(ats, 'execute', side_effect=execute)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    @staticmethod
    def git(*args, cwd):
        return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, check=True).stdout

    def identity(self, folder):
        self.git('config', 'user.name', 't', cwd=folder)
        self.git('config', 'user.email', 't@t', cwd=folder)

    def write(self, rel, text, folder=None):
        file = (folder or self.local) / rel
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)

    def merge_pr(self):
        """What the human does on GitHub: merge the sync branch into main."""
        self.git('pull', '-q', 'origin', 'main', cwd=self.other)
        self.git('fetch', '-q', 'origin', 'ats-data/sync-pc', cwd=self.other)
        self.git('merge', '-q', '--no-ff', '-m', 'merge PR', 'FETCH_HEAD', cwd=self.other)
        self.git('push', '-q', 'origin', 'main', cwd=self.other)

    def branch_files(self):
        self.git('fetch', '-q', 'origin', cwd=self.local)
        return set(self.git('ls-tree', '-r', '--name-only', 'origin/ats-data/sync-pc', cwd=self.local).split())

    def test_one_rolling_pr_that_never_conflicts_and_keeps_local_data(self):
        self.write('data/ARC/events/e1.json', '{"n": 1}\n')
        self.write('knowledge/decided.json', '{"status": "approved", "at": 1}\n')
        ats.sync_private_data()
        self.assertEqual(len(self.prs), 1)
        self.assertEqual(self.git('branch', '--show-current', cwd=self.local).strip(), 'main')
        self.assertTrue((self.local / 'data/ARC/events/e1.json').exists())   # nothing vanished locally
        # more work and the same decision taken again later: the SAME PR is updated, not a new one
        self.write('data/ARC/events/e2.json', '{"n": 2}\n')
        self.write('knowledge/decided.json', '{"status": "approved", "at": 2}\n')
        ats.sync_private_data()
        self.assertEqual(len(self.prs), 1)
        self.assertEqual(self.branch_files(), {'knowledge/decided.json', 'data/ARC/events/e1.json',
                                               'data/ARC/events/e2.json'})
        # meanwhile main moved on (another PR merged): the sync branch is rebuilt on top of it
        self.write('knowledge/other.json', '{}\n', folder=self.other)
        self.git('add', '-A', cwd=self.other)
        self.git('commit', '-q', '-m', 'other', cwd=self.other)
        self.git('push', '-q', 'origin', 'main', cwd=self.other)
        ats.sync_private_data()
        self.merge_pr()   # merges without conflicts
        merged = self.git('show', 'origin/main:knowledge/decided.json', cwd=self.other)
        self.assertIn('"at": 2', merged)
        # after the merge the local copies are identical to main: clean, nothing left to publish
        ats.sync_private_data()
        self.assertEqual(self.git('status', '--porcelain', cwd=self.local), '')
        self.assertTrue((self.local / 'knowledge/other.json').exists())

    def test_refresh_keeps_a_newer_local_version_of_a_file_changed_upstream(self):
        self.write('knowledge/decided.json', '{"status": "rejected"}\n', folder=self.other)
        self.git('commit', '-qam', 'upstream decision', cwd=self.other)
        self.git('push', '-q', 'origin', 'main', cwd=self.other)
        self.write('knowledge/decided.json', '{"status": "approved"}\n')
        self.write('data/ARC/events/e1.json', '{}\n')
        ats.refresh_private_knowledge()
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=self.local),
                         self.git('rev-parse', 'origin/main', cwd=self.local))
        self.assertIn('approved', (self.local / 'knowledge/decided.json').read_text())
        self.assertTrue((self.local / 'data/ARC/events/e1.json').exists())

    def test_changes_outside_data_folders_are_refused(self):
        self.write('README.md', 'local edit\n')
        with self.assertRaisesRegex(RuntimeError, 'outside data/ and knowledge/'):
            ats.sync_private_data()
        self.assertEqual(self.prs, [])


if __name__ == '__main__':
    unittest.main()
