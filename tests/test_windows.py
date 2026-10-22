import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats


class WindowsSupportTests(unittest.TestCase):
    def test_windows_prefers_cmd_launcher(self):
        def which(name):
            return 'C:/Tools/' + name if name.endswith('.cmd') else None
        with patch.object(ats.os, 'name', 'nt'), patch.object(ats.shutil, 'which', side_effect=which):
            self.assertTrue(ats.executable('codex').endswith('codex.cmd'))
            self.assertTrue(ats.executable('npm').endswith('npm.cmd'))
            self.assertEqual(ats.agent_command('claude', '-p', 'test')[1:], ['-p', 'test'])

    def test_windows_checks_use_shell(self):
        cwd = pathlib.Path('.')
        with patch.object(ats.os, 'name', 'nt'), patch.object(ats.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            ats.run_check('python -m unittest', cwd)
            self.assertTrue(run.call_args.kwargs['shell'])

    def test_unix_checks_do_not_use_shell(self):
        with patch.object(ats.os, 'name', 'posix'), patch.object(ats.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            ats.run_check('python3 -m unittest', pathlib.Path('.'))
            self.assertFalse(run.call_args.kwargs['shell'])


if __name__ == '__main__':
    unittest.main()
