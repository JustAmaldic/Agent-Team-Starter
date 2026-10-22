import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats


class LocalWorkspaceTests(unittest.TestCase):
    def test_secret_and_generated_paths_are_excluded(self):
        names = ['.ats', '.git', '.venv', 'node_modules', '.env', '.env.local',
                 '.env.example', 'id_rsa', 'service.key', 'main.py', 'README.md']
        excluded = ats.non_git_copy_ignore('.', names)
        self.assertTrue({'.ats', '.git', '.venv', 'node_modules', '.env',
                         '.env.local', 'id_rsa', 'service.key'} <= excluded)
        self.assertNotIn('.env.example', excluded)
        self.assertNotIn('main.py', excluded)

    def test_creates_local_snapshot_without_changing_source(self):
        with tempfile.TemporaryDirectory() as root:
            app = pathlib.Path(root) / 'app'
            app.mkdir()
            (app / 'hello.py').write_text('print("old")\n')
            (app / '.env').write_text('secret')
            (app / '.gitignore').write_text('.env\n')
            (app / '.ats').mkdir()
            (app / '.ats' / 'POLICY.md').write_text('policy')
            workspace = pathlib.Path(root) / 'work'
            with patch.object(ats, 'APP', app):
                ats.make_local_workspace(workspace, 'ats/test-local')
                self.assertFalse((workspace / '.env').exists())
                self.assertFalse((workspace / '.ats').exists())
                self.assertTrue((workspace / '.git').exists())
                (workspace / 'hello.py').write_text('print("new")\n')
                subprocess.run(['git', 'add', '-A'], cwd=workspace, check=True)
                subprocess.run(['git', '-c', 'user.name=ATS',
                                '-c', 'user.email=ats@localhost',
                                'commit', '-m', 'change'], cwd=workspace,
                               check=True, capture_output=True)
                patch_file = ats.export_local_patch(workspace)
                self.assertTrue(patch_file.exists())
                self.assertIn('print("new")', patch_file.read_text())
                self.assertEqual((app / 'hello.py').read_text(), 'print("old")\n')

    def test_rejects_push_for_non_git_project(self):
        with tempfile.TemporaryDirectory() as root:
            app = pathlib.Path(root) / 'plain'
            app.mkdir()
            with patch.object(ats, 'APP', app), \
                 patch.object(ats, 'project', return_value={}):
                with self.assertRaisesRegex(RuntimeError, 'without --push'):
                    ats.task_flow('fix issue', 'codex', push=True)


if __name__ == '__main__':
    unittest.main()
