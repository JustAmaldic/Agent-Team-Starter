"""Public export ships a license and starts history with a GitHub no-reply identity."""
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import export_public

NOREPLY = '12345+octocat@users.noreply.github.com'


def git_config(folder, key):
    return subprocess.run(['git', 'config', '--local', key], cwd=folder, text=True,
                          capture_output=True).stdout.strip()


class PublicExportIdentityTests(unittest.TestCase):
    def test_export_contains_mit_license(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = export_public.export(pathlib.Path(temp) / 'public')
            text = (destination / 'LICENSE').read_text(encoding='utf-8')
        self.assertTrue(text.startswith('MIT License'))

    def test_init_git_uses_noreply_identity_locally(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = export_public.export(pathlib.Path(temp) / 'public')
            export_public.init_git(destination, 'octocat', NOREPLY)
            self.assertEqual(git_config(destination, 'user.email'), NOREPLY)
            self.assertEqual(git_config(destination, 'user.name'), 'octocat')
            self.assertEqual(subprocess.run(['git', 'rev-list', '--all'], cwd=destination,
                                            text=True, capture_output=True).stdout, '')

    def test_init_git_refuses_personal_address(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = export_public.export(pathlib.Path(temp) / 'public')
            with self.assertRaisesRegex(RuntimeError, 'non-no-reply'):
                export_public.init_git(destination, 'Someone', 'someone@example.com')
            self.assertFalse((destination / '.git').exists())

    def test_identity_is_looked_up_via_gh(self):
        result = subprocess.CompletedProcess([], 0, 'octocat\n12345\n', '')
        with patch.object(export_public.shutil, 'which', return_value='gh'), \
             patch.object(export_public.subprocess, 'run', return_value=result):
            self.assertEqual(export_public.github_noreply_identity(), ('octocat', NOREPLY))


if __name__ == '__main__':
    unittest.main()
