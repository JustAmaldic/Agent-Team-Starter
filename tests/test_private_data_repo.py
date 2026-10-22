import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats


class PrivateDataRepositoryTests(unittest.TestCase):
    def test_opt_in_data_repo_uses_shared_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / '.git').mkdir()
            with patch.dict('os.environ', {'ATS_DATA_REPO': str(root)}):
                self.assertEqual(ats.private_data_repo(), root.resolve())

    def test_data_repo_refuses_non_git_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict('os.environ', {'ATS_DATA_REPO': tmp}):
                with self.assertRaisesRegex(RuntimeError, 'not a Git clone'):
                    ats.private_data_repo()

    def test_sync_refuses_public_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            def fake_exec(cmd, cwd=None, **kwargs):
                if cmd[:3] == ['git', 'remote', 'get-url']:
                    return subprocess.CompletedProcess(cmd, 0, 'https://github.com/example-owner/Agent-Team-Starter.git\n', '')
                self.fail('Unexpected command: ' + str(cmd))
            with patch.object(ats, 'PRIVATE_DATA_REPO', folder), \
                 patch.object(ats, 'execute', side_effect=fake_exec):
                with self.assertRaisesRegex(RuntimeError, 'unexpected remote'):
                    ats.sync_private_data()

    def test_sync_rejects_non_private_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            def fake_exec(cmd, cwd=None, **kwargs):
                if cmd[:3] == ['git', 'remote', 'get-url']:
                    return subprocess.CompletedProcess(cmd, 0, 'https://github.com/example-owner/ats-private-data.git\n', '')
                if 'repo' in cmd and 'view' in cmd:
                    return subprocess.CompletedProcess(cmd, 0, 'PUBLIC\n', '')
                self.fail('Unexpected command: ' + str(cmd))
            with patch.object(ats, 'PRIVATE_DATA_REPO', folder), \
                 patch.dict('os.environ', {'ATS_DATA_REMOTE': 'example-owner/ats-private-data'}), \
                 patch.object(ats.shutil, 'which', return_value='gh'), \
                 patch.object(ats, 'execute', side_effect=fake_exec):
                with self.assertRaisesRegex(RuntimeError, 'not verified PRIVATE'):
                    ats.sync_private_data()


if __name__ == '__main__':
    unittest.main()
