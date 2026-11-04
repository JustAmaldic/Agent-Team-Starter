import os
import pathlib
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats



_plain = None


def setUpModule():
    """These tests cover the plain-text CLI contract; JSON output is covered in test_token_efficiency."""
    global _plain
    _plain = patch.dict(os.environ, {'ATS_STRUCTURED_OUTPUT': '0'})
    _plain.start()


def tearDownModule():
    _plain.stop()

class CodexCLITest(unittest.TestCase):
    def test_invoke_uses_approval_aware_command(self):
        seen = {}

        def fake_stream(command, cwd, timeout, quiet, stdin_text=None):
            seen['command'] = command
            seen['stdin'] = stdin_text
            seen['cwd'] = cwd
            return subprocess.CompletedProcess(command, 0, 'ok', '')

        with patch.object(ats, 'executable', return_value='/usr/local/bin/codex'), \
             patch.object(ats, 'stream_command', side_effect=fake_stream):
            result = ats.invoke('codex', 'Fix the login bug', pathlib.Path('/tmp/project'))

        self.assertEqual(result.strip(), 'ok')
        self.assertEqual(seen['command'], [
            '/usr/local/bin/codex', 'exec', '--approve-for-me', '-'
        ])
        self.assertEqual(seen['stdin'], 'Fix the login bug')
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', seen['command'])


    def test_long_multiline_prompt_never_goes_on_the_command_line(self):
        prompt = 'line one\n"quoted" & | <>\n' + 'x' * 200_000
        for agent in ('codex', 'claude'):
            seen = {}

            def fake_stream(command, cwd, timeout, quiet, stdin_text=None):
                seen.update(command=command, stdin=stdin_text)
                return subprocess.CompletedProcess(command, 0, 'ok', '')

            with patch.object(ats, 'executable', return_value=agent), \
                 patch.object(ats, 'stream_command', side_effect=fake_stream):
                ats.invoke(agent, prompt, pathlib.Path('.'))
            self.assertEqual(seen['stdin'], prompt)
            self.assertNotIn('quoted', ' '.join(seen['command']), agent)
            self.assertLess(len(' '.join(seen['command'])), 1000, agent)   # fixed flags only, never the prompt

    def test_stream_command_feeds_stdin(self):
        prompt = 'regel 1\nregel 2 met é\n' + 'y' * 100_000
        code = 'import sys; data = sys.stdin.read(); print(len(data), data.splitlines()[1])'
        result = ats.stream_command([sys.executable, '-X', 'utf8', '-c', code], '.', quiet=True, stdin_text=prompt)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f'{len(prompt)} regel 2 met é')


if __name__ == '__main__':
    unittest.main()
