import json
import pathlib
import subprocess
import unittest
from unittest.mock import patch

import ats


class AntigravityProviderTests(unittest.TestCase):
    def test_supported_but_not_default(self):
        self.assertIn('antigravity', ats.SUPPORTED_AGENTS)
        self.assertNotIn('antigravity', ats.DEFAULT_AGENTS)

    def test_headless_success_parsed_as_text(self):
        response = ats.parse_antigravity_result(json.dumps({
            'status': 'SUCCESS', 'response': 'VERDICT: approved', 'usage': {'total_tokens': 42}
        }))
        self.assertEqual(response, 'VERDICT: approved')

    def test_cli_status_error_even_when_exit_zero(self):
        with self.assertRaisesRegex(RuntimeError, 'headless status ERROR'):
            ats.parse_antigravity_result(json.dumps({'status': 'ERROR', 'error': 'invalid model'}))

    def test_permission_soft_denial_does_not_count_as_success(self):
        with self.assertRaises(ats.AgentSandboxBlocked):
            ats.parse_antigravity_result(json.dumps({'status': 'SUCCESS', 'response': 'done'}),
                                        'Tool requires approval; soft-denied')

    def test_usage_exhaustion_is_detected(self):
        with self.assertRaises(ats.AgentUsageExhausted):
            ats.parse_antigravity_result(json.dumps({
                'status': 'ERROR', 'error': 'usage limit exceeded'
            }))

    def test_headless_command_uses_safe_modes_and_no_unrestricted_permissions(self):
        calls = []
        def fake_stream(command, cwd, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, json.dumps({
                'status': 'SUCCESS', 'response': 'done'
            }), '')
        with patch.object(ats, 'executable', return_value='/usr/bin/agy'), \
             patch.object(ats, 'stream_command', side_effect=fake_stream):
            ats.invoke_antigravity('Implement fix', pathlib.Path('.'), purpose='implementation')
            ats.invoke_antigravity('Review fix', pathlib.Path('.'), purpose='review')
        self.assertIn('--mode=accept-edits', calls[0])
        self.assertIn('--mode=plan', calls[1])
        self.assertIn('--output-format', calls[0])
        self.assertNotIn('--dangerously-skip-permissions', calls[0])

    def test_windows_uses_agy_shim(self):
        with patch.object(ats.os, 'name', 'nt'), \
             patch.object(ats.shutil, 'which', side_effect=lambda name: 'C:/agy.cmd' if name == 'agy.cmd' else None):
            self.assertEqual(ats.executable('antigravity'), 'C:/agy.cmd')


if __name__ == '__main__':
    unittest.main()
