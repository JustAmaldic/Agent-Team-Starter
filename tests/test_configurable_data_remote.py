"""The private data repository is configurable (ATS_DATA_REMOTE) instead of hard-coded."""
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats
from scripts import bootstrap_private_data

DATA = pathlib.Path('/tmp/ats-data-clone')


def remotes(data_url, source_url='https://github.com/someone/Agent-Team-Starter.git',
            visibility='PRIVATE'):
    def fake(cmd, cwd=None, **kwargs):
        if cmd[:3] == ['git', 'remote', 'get-url']:
            return subprocess.CompletedProcess(cmd, 0, (source_url if cwd == ats.ATS else data_url) + '\n', '')
        if 'repo' in cmd and 'view' in cmd:
            fake.viewed = cmd[cmd.index('view') + 1]
            return subprocess.CompletedProcess(cmd, 0, visibility + '\n', '')
        raise AssertionError('unexpected command: ' + str(cmd))
    fake.viewed = None
    return fake


class DataRemoteTests(unittest.TestCase):
    def test_no_owner_hard_coded_in_source(self):
        source = (pathlib.Path(ats.__file__)).read_text(encoding='utf-8')
        self.assertNotIn('JustAmaldic', source)
        script = pathlib.Path(bootstrap_private_data.__file__).read_text(encoding='utf-8')
        self.assertNotIn('JustAmaldic', script)

    def test_github_repo_parsing(self):
        for url in ('https://github.com/o/r.git', 'git@github.com:o/r.git',
                    'ssh://git@github.com/o/r', 'https://github.com/o/r/'):
            self.assertEqual(ats.github_repo(url), 'o/r')
        self.assertIsNone(ats.github_repo('https://gitlab.com/o/r.git'))
        self.assertIsNone(ats.github_repo('o/r'))
        self.assertEqual(ats.github_repo('o/r', allow_bare=True), 'o/r')

    def test_without_setting_origin_of_data_clone_is_used(self):
        fake = remotes('git@github.com:alice/my-ats-data.git')
        with patch.dict('os.environ', {}, clear=False), \
             patch.object(ats, 'execute', side_effect=fake), \
             patch.object(ats.shutil, 'which', return_value='gh'):
            ats.os.environ.pop('ATS_DATA_REMOTE', None)
            ats.validate_private_remote(DATA)
        self.assertEqual(fake.viewed, 'alice/my-ats-data')

    def test_data_clone_pointing_at_ats_source_is_refused(self):
        fake = remotes('https://github.com/someone/Agent-Team-Starter.git')
        with patch.object(ats, 'execute', side_effect=fake):
            ats.os.environ.pop('ATS_DATA_REMOTE', None)
            with self.assertRaisesRegex(RuntimeError, 'ATS source repository'):
                ats.validate_private_remote(DATA)

    def test_configured_remote_accepts_url_form_and_rejects_mismatch(self):
        with patch.dict('os.environ', {'ATS_DATA_REMOTE': 'https://github.com/alice/my-ats-data.git'}), \
             patch.object(ats.shutil, 'which', return_value='gh'):
            with patch.object(ats, 'execute', side_effect=remotes('git@github.com:alice/my-ats-data.git')):
                ats.validate_private_remote(DATA)
            with patch.object(ats, 'execute', side_effect=remotes('git@github.com:mallory/x.git')):
                with self.assertRaisesRegex(RuntimeError, 'unexpected remote'):
                    ats.validate_private_remote(DATA)

    def test_invalid_setting_and_non_github_remote_are_refused(self):
        with patch.dict('os.environ', {'ATS_DATA_REMOTE': 'not a repo'}), \
             patch.object(ats, 'execute', side_effect=remotes('git@github.com:a/b.git')):
            with self.assertRaisesRegex(RuntimeError, 'ATS_DATA_REMOTE must be'):
                ats.validate_private_remote(DATA)
        with patch.object(ats, 'execute', side_effect=remotes('https://gitlab.com/a/b.git')):
            ats.os.environ.pop('ATS_DATA_REMOTE', None)
            with self.assertRaisesRegex(RuntimeError, 'not a GitHub repository'):
                ats.validate_private_remote(DATA)

    def test_public_data_repo_is_refused(self):
        with patch.object(ats, 'execute', side_effect=remotes('git@github.com:a/b.git', visibility='PUBLIC')), \
             patch.object(ats.shutil, 'which', return_value='gh'):
            ats.os.environ.pop('ATS_DATA_REMOTE', None)
            with self.assertRaisesRegex(RuntimeError, 'a/b is not verified PRIVATE'):
                ats.validate_private_remote(DATA)


class BootstrapTests(unittest.TestCase):
    def test_bootstrap_requires_repo(self):
        with patch.dict('os.environ', {}, clear=False):
            bootstrap_private_data.os.environ.pop('ATS_DATA_REMOTE', None)
            with tempfile.TemporaryDirectory() as temp:
                with self.assertRaisesRegex(RuntimeError, '--repo OWNER/REPO'):
                    bootstrap_private_data.bootstrap(pathlib.Path(temp) / 'x', pathlib.Path(temp))


if __name__ == '__main__':
    unittest.main()
