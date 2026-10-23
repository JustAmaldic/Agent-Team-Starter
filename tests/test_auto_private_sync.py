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

    def test_refresh_pulls_only_clean_private_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            calls = []
            def fake_execute(cmd, cwd=None, **kwargs):
                calls.append(cmd)
                if cmd[:3] == ['git', 'branch', '--show-current']:
                    return proc(cmd, 'main\n')
                if cmd[:2] == ['git', 'status']:
                    return proc(cmd, '')
                return proc(cmd)
            with patch.object(ats, 'PRIVATE_DATA_REPO', pathlib.Path(temp)), \
                 patch.object(ats, 'validate_private_remote'), \
                 patch.object(ats, 'execute', side_effect=fake_execute):
                ats.refresh_private_knowledge()
            self.assertIn(['git', 'pull', '--ff-only', 'origin', 'main'], calls)

    def test_refresh_does_not_pull_over_uncommitted_events(self):
        with tempfile.TemporaryDirectory() as temp:
            calls = []
            def fake_execute(cmd, cwd=None, **kwargs):
                calls.append(cmd)
                if cmd[:3] == ['git', 'branch', '--show-current']:
                    return proc(cmd, 'main\n')
                if cmd[:2] == ['git', 'status']:
                    return proc(cmd, '?? data/ARC/events/new.json\n')
                return proc(cmd)
            with patch.object(ats, 'PRIVATE_DATA_REPO', pathlib.Path(temp)), \
                 patch.object(ats, 'validate_private_remote'), \
                 patch.object(ats, 'execute', side_effect=fake_execute):
                ats.refresh_private_knowledge()
            self.assertNotIn(['git', 'pull', '--ff-only', 'origin', 'main'], calls)

    def test_sync_commits_to_feature_branch_opens_pr_and_returns_to_main(self):
        with tempfile.TemporaryDirectory() as temp:
            calls = []
            def fake_execute(cmd, cwd=None, **kwargs):
                calls.append(cmd)
                if cmd[:3] == ['git', 'branch', '--show-current']:
                    return proc(cmd, 'main\n')
                if cmd[:2] == ['git', 'status']:
                    if '--untracked-files=all' in cmd:
                        return proc(cmd, '?? knowledge/projects/ARC/findings/new.json\n')
                    return proc(cmd, '')
                if cmd[:3] == ['git', 'diff', '--cached']:
                    return proc(cmd, 'knowledge/projects/ARC/findings/new.json\n')
                if cmd[:3] == ['git', 'remote', 'get-url']:
                    if cwd == ats.ATS:
                        return proc(cmd, 'https://github.com/someone/Agent-Team-Starter.git\n')
                    return proc(cmd, 'git@github.com:someone/ats-private.git\n')
                if cmd[:3] == ['/usr/bin/gh', 'pr', 'create']:
                    self.assertEqual(cmd[cmd.index('--repo') + 1], 'someone/ats-private')
                    return proc(cmd, 'https://github.com/someone/ats-private/pull/2\n')
                return proc(cmd)
            with patch.object(ats, 'PRIVATE_DATA_REPO', pathlib.Path(temp)), \
                 patch.object(ats, 'validate_private_remote', return_value='/usr/bin/gh'), \
                 patch.object(ats, 'execute', side_effect=fake_execute):
                ats.sync_private_data()
            self.assertTrue(any(cmd[:3] == ['git', 'switch', '-c'] for cmd in calls))
            self.assertIn(['git', 'switch', 'main'], calls)
            self.assertTrue(any(cmd[:3] == ['/usr/bin/gh', 'pr', 'create'] for cmd in calls))
            self.assertNotIn(['git', 'push', 'origin', 'main'], calls)

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


if __name__ == '__main__':
    unittest.main()
