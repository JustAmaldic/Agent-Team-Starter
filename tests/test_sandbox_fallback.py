import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats


class SandboxFallbackTests(unittest.TestCase):
    def test_codex_command_uses_approval_aware_sandbox(self):
        with patch.object(ats, 'agent_command', side_effect=lambda *args: list(args)):
            self.assertEqual(
                ats.codex_exec_command('hello'),
                ['codex', 'exec', '--approve-for-me', 'hello'])
            self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',
                             ats.codex_exec_command('hello'))

    def test_preflight_uses_same_command_as_implementation(self):
        with tempfile.TemporaryDirectory() as folder:
            seen = []
            def fake_stream(command, cwd, timeout, quiet):
                seen.append(command)
                (pathlib.Path(cwd) / '.ats-codex-preflight').write_text('ATS_PREFLIGHT_OK')
                return subprocess.CompletedProcess(command, 0, 'done', '')
            with patch.object(ats, 'agent_command', side_effect=lambda *args: list(args)), \
                 patch.object(ats, 'executable', return_value='codex'), \
                 patch.object(ats, 'stream_command', side_effect=fake_stream):
                ats.codex_preflight(folder, quiet=True)
                ats.invoke('codex', 'implement task', folder, quiet=True)
            self.assertEqual(seen[0][:3], ['codex', 'exec', '--approve-for-me'])
            self.assertEqual(seen[1], ['codex', 'exec', '--approve-for-me', 'implement task'])

    def test_detects_windows_policy_block(self):
        self.assertTrue(ats.sandbox_blocked('CreateProcess rejected: blocked by policy'))
        self.assertTrue(ats.sandbox_blocked('sandbox: read-only'))
        self.assertFalse(ats.sandbox_blocked('Completed successfully'))

    def test_preflight_rejects_no_write_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as folder:
            response = subprocess.CompletedProcess([], 0, 'done', '')
            with patch.object(ats, 'stream_command', return_value=response):
                with self.assertRaises(ats.AgentSandboxBlocked):
                    ats.codex_preflight(folder, quiet=True)
            self.assertFalse((pathlib.Path(folder) / '.ats-codex-preflight').exists())

    def test_preflight_accepts_verified_write(self):
        with tempfile.TemporaryDirectory() as folder:
            def fake_stream(command, cwd, timeout, quiet):
                (pathlib.Path(cwd) / '.ats-codex-preflight').write_text('ATS_PREFLIGHT_OK')
                return subprocess.CompletedProcess(command, 0, 'done', '')
            with patch.object(ats, 'stream_command', side_effect=fake_stream):
                ats.codex_preflight(folder, quiet=True)
            self.assertFalse((pathlib.Path(folder) / '.ats-codex-preflight').exists())

    def test_falls_back_to_claude_after_codex_preflight_block(self):
        with patch.object(ats, 'codex_preflight', side_effect=ats.AgentSandboxBlocked('blocked')), \
             patch.object(ats, 'invoke', return_value='Claude succeeded') as invoke, \
             patch.object(ats, 'record'):
            agent, result = ats.invoke_with_fallback(
                'codex', 'task', '.', set(), 'task-id', 'implementation', quiet=True)
        self.assertEqual(agent, 'claude')
        self.assertEqual(result, 'Claude succeeded')
        self.assertEqual(invoke.call_args.args[0], 'claude')


if __name__ == '__main__':
    unittest.main()
