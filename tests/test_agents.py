import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats


class AgentTests(unittest.TestCase):
    def test_usage_detection(self):
        for message in ('You have reached your usage limit', 'rate limit exceeded',
                        'insufficient_quota', 'Credit balance is too low'):
            self.assertTrue(ats.is_usage_exhausted(message))
        for message in ('Invalid API key', 'SyntaxError', 'permission denied', 'Not logged in'):
            self.assertFalse(ats.is_usage_exhausted(message))

    def test_fallback_on_exhausted_codex(self):
        def invoke(agent, prompt, cwd, quiet=False):
            if agent == 'codex':
                raise ats.AgentUsageExhausted(agent)
            return 'Claude completed'
        with patch.object(ats, 'invoke', side_effect=invoke), patch.object(ats, 'record'), patch.object(ats, 'codex_preflight'):
            unavailable = set()
            agent, result = ats.invoke_with_fallback('codex', 'task', pathlib.Path('.'), unavailable, 'id', 'implementation')
            self.assertEqual(agent, 'claude')
            self.assertIn('codex', unavailable)
            self.assertEqual(result, 'Claude completed')

    def test_both_exhausted(self):
        with patch.object(ats, 'invoke', side_effect=lambda a, *args, **kwargs: (_ for _ in ()).throw(ats.AgentUsageExhausted(a))), patch.object(ats, 'record'), patch.object(ats, 'codex_preflight'):
            with self.assertRaisesRegex(ats.BothAgentsExhausted, 'Usage voor beide agents is op'):
                ats.invoke_with_fallback('claude', 'task', pathlib.Path('.'), set(), 'id', 'implementation')

    def test_claude_status(self):
        self.assertTrue(ats.claude_authenticated('{"loggedIn": true}'))
        self.assertFalse(ats.claude_authenticated('{"loggedIn": false}'))


if __name__ == '__main__':
    unittest.main()
