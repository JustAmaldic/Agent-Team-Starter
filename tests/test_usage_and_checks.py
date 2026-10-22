"""Usage-limit detection, rate-limit retry and test-command execution."""
import pathlib
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats

# Messages observed from the agent CLIs (the first one was seen in real ATS task events).
HARD_LIMIT_MESSAGES = (
    "You've hit your weekly limit · resets 11am (Europe/Amsterdam)",
    "Claude AI usage limit reached|1761130800",
    "5-hour limit reached ∙ resets 4pm",
    "You've hit your usage limit. Upgrade to Pro or try again at 3:05 PM.",
)


class UsageDetectionTests(unittest.TestCase):
    def test_real_cli_limit_messages_are_hard_limits(self):
        for message in HARD_LIMIT_MESSAGES:
            with self.subTest(message=message):
                self.assertTrue(ats.is_hard_usage_limit(message))
                self.assertTrue(ats.is_usage_exhausted(message))
                self.assertFalse(ats.is_transient_rate_limit(message))

    def test_throttling_is_transient(self):
        for message in ('429 Too Many Requests', 'API Error: Overloaded (529)', 'rate limit exceeded'):
            with self.subTest(message=message):
                self.assertTrue(ats.is_transient_rate_limit(message))

    def test_unrelated_errors_are_not_usage(self):
        for message in ('Invalid API key', 'Not logged in', 'SyntaxError at line 429x',
                        'unexpected argument --full-auto'):
            with self.subTest(message=message):
                self.assertFalse(ats.is_usage_exhausted(message))

    def test_reset_hint_is_extracted(self):
        self.assertEqual(ats.usage_reset_hint(HARD_LIMIT_MESSAGES[0]),
                         'resets 11am (Europe/Amsterdam)')
        self.assertEqual(ats.usage_reset_hint(HARD_LIMIT_MESSAGES[3]), 'try again at 3:05 PM')
        self.assertIsNone(ats.usage_reset_hint('no hint here'))


def completed(code, out=''):
    return subprocess.CompletedProcess(['x'], code, out, '')


class InvokeRetryTests(unittest.TestCase):
    def run_invoke(self, outputs, backoff=(0, 0)):
        with patch.object(ats, 'stream_command', side_effect=outputs) as stream, \
             patch.object(ats, 'executable', return_value='/bin/claude'), \
             patch.object(ats.time, 'sleep') as sleep:
            try:
                return ats.invoke('claude', 'p', '.', quiet=True, backoff=backoff), stream, sleep
            except Exception as exc:  # noqa: BLE001 - returned for assertions
                return exc, stream, sleep

    def test_transient_limit_is_retried_then_succeeds(self):
        result, stream, sleep = self.run_invoke([completed(1, '429 Too Many Requests'),
                                                 completed(0, 'done')])
        self.assertIn('done', result)
        self.assertEqual(stream.call_count, 2)
        self.assertEqual(sleep.call_count, 1)

    def test_transient_limit_gives_up_after_backoff(self):
        result, stream, _ = self.run_invoke([completed(1, 'Too many requests')] * 3)
        self.assertIsInstance(result, ats.AgentUsageExhausted)
        self.assertEqual(stream.call_count, 3)

    def test_hard_limit_is_not_retried_and_keeps_reset_hint(self):
        result, stream, sleep = self.run_invoke([completed(1, HARD_LIMIT_MESSAGES[0])])
        self.assertIsInstance(result, ats.AgentUsageExhausted)
        self.assertEqual(result.resets, 'resets 11am (Europe/Amsterdam)')
        self.assertIn('usage voor claude is op', str(result))
        self.assertEqual(stream.call_count, 1)
        sleep.assert_not_called()


class FallbackTests(unittest.TestCase):
    def test_both_exhausted_reports_reset_times(self):
        def invoke(agent, *args, **kwargs):
            raise ats.AgentUsageExhausted(agent, 'resets 11am' if agent == 'claude' else None)
        with patch.object(ats, 'invoke', side_effect=invoke), patch.object(ats, 'record'), \
             patch.object(ats, 'codex_preflight'):
            with self.assertRaises(ats.BothAgentsExhausted) as ctx:
                ats.invoke_with_fallback('codex', 't', '.', ats.UnavailableAgents(), 'id',
                                         'implementation', quiet=True)
        self.assertIn('claude: resets 11am', str(ctx.exception))

    def test_sandbox_plus_usage_is_not_reported_as_usage(self):
        def invoke(agent, *args, **kwargs):
            if agent == 'codex':
                raise ats.AgentSandboxBlocked('blocked')
            raise ats.AgentUsageExhausted(agent)
        with patch.object(ats, 'invoke', side_effect=invoke), patch.object(ats, 'record'), \
             patch.object(ats, 'codex_preflight'):
            with self.assertRaises(RuntimeError) as ctx:
                ats.invoke_with_fallback('codex', 't', '.', ats.UnavailableAgents(), 'id',
                                         'implementation', quiet=True)
        self.assertNotIsInstance(ctx.exception, ats.AgentUsageExhausted)
        self.assertIn('codex sandbox', str(ctx.exception))

    def test_previously_exhausted_agent_counts_as_usage(self):
        unavailable = ats.UnavailableAgents()
        ats.mark_unavailable(unavailable, 'claude', 'usage')
        with patch.object(ats, 'invoke', side_effect=ats.AgentUsageExhausted('codex')), \
             patch.object(ats, 'record'):
            with self.assertRaises(ats.BothAgentsExhausted):
                ats.invoke_with_fallback('claude', 't', '.', unavailable, 'id', 'review', quiet=True)


class RunCheckTests(unittest.TestCase):
    def test_posix_string_command_actually_runs(self):
        if ats.os.name == 'nt':
            self.skipTest('POSIX behaviour')
        result = ats.run_check(f'"{sys.executable}" -c "print(41 + 1)"', pathlib.Path('.'))
        self.assertEqual(result.returncode, 0)
        self.assertIn('42', result.stdout)

    def test_posix_string_is_split_without_shell(self):
        with patch.object(ats.os, 'name', 'posix'), \
             patch.object(ats.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            ats.run_check('python3 -m pytest -q', pathlib.Path('.'))
        self.assertEqual(run.call_args.args[0], ['python3', '-m', 'pytest', '-q'])
        self.assertFalse(run.call_args.kwargs['shell'])

    def test_list_command_is_supported(self):
        result = ats.run_check([sys.executable, '-c', 'import sys; sys.exit(3)'], pathlib.Path('.'))
        self.assertEqual(result.returncode, 3)

    def test_timeout_becomes_failed_check(self):
        result = ats.run_check([sys.executable, '-c', 'import time; time.sleep(5)'],
                               pathlib.Path('.'), timeout=0.5)
        self.assertEqual(result.returncode, 124)
        self.assertIn('timed out', result.stderr)

    def test_missing_executable_becomes_failed_check(self):
        if ats.os.name == 'nt':
            self.skipTest('Windows runs strings through the shell')
        with tempfile.TemporaryDirectory() as temp:
            result = ats.run_check('definitely-not-a-real-binary --x', pathlib.Path(temp))
        self.assertEqual(result.returncode, 127)


if __name__ == '__main__':
    unittest.main()
