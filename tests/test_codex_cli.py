import pathlib
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats


class CodexCLITest(unittest.TestCase):
    def test_invoke_uses_approval_aware_command(self):
        seen = {}

        def fake_stream(command, cwd, timeout, quiet):
            seen['command'] = command
            seen['cwd'] = cwd
            return subprocess.CompletedProcess(command, 0, 'ok', '')

        with patch.object(ats, 'executable', return_value='/usr/local/bin/codex'), \
             patch.object(ats, 'stream_command', side_effect=fake_stream):
            result = ats.invoke('codex', 'Fix the login bug', pathlib.Path('/tmp/project'))

        self.assertEqual(result.strip(), 'ok')
        self.assertEqual(seen['command'], [
            '/usr/local/bin/codex', 'exec', '--approve-for-me', 'Fix the login bug'
        ])
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', seen['command'])


if __name__ == '__main__':
    unittest.main()
