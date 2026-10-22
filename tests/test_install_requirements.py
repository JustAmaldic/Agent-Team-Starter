import pathlib
import sys
import unittest
from unittest.mock import patch, call

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats


class RequirementsInstallTests(unittest.TestCase):
    def test_empty_requirements_skips_pip(self):
        with patch.object(ats, 'executable', return_value='npm'), patch.object(ats.shutil, 'which', return_value='git'), patch.object(ats, 'execute') as execute:
            ats.install_requirements()
            execute.assert_not_called()

    def test_windows_missing_npm_uses_winget(self):
        def which(name):
            return 'C:/winget.exe' if name == 'winget' else 'C:/git.exe' if name == 'git' else None
        with patch.object(ats.os, 'name', 'nt'), patch.object(ats, 'executable', side_effect=[None, None]), patch.object(ats.shutil, 'which', side_effect=which), patch.object(ats, 'execute') as execute:
            with self.assertRaisesRegex(RuntimeError, 'reopen|new terminal'):
                ats.install_requirements()
            command = execute.call_args.args[0]
            self.assertIn('OpenJS.NodeJS.LTS', command)
            self.assertNotIn('brew', command)

    def test_install_orders_prerequisites_before_agents(self):
        with patch.object(ats, 'sync_in'), patch.object(ats, 'sync_out'), patch.object(ats, 'install_requirements') as req, patch.object(ats, 'install_agents') as agents, patch.object(sys, 'argv', ['ats.py', 'install']):
            ats.main()
            self.assertTrue(req.called)
            self.assertTrue(agents.called)


if __name__ == '__main__':
    unittest.main()
