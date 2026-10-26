import unittest

import ats
from ats_modules import agent_limits


class ModularAgentLimitsTests(unittest.TestCase):
    def test_legacy_api_reexports_the_same_classifiers(self):
        self.assertIs(ats.AgentUsageExhausted, agent_limits.AgentUsageExhausted)
        self.assertIs(ats.is_hard_usage_limit, agent_limits.is_hard_usage_limit)
        self.assertIs(ats.sandbox_blocked, agent_limits.sandbox_blocked)

    def test_quota_and_sandbox_detection(self):
        self.assertTrue(ats.is_hard_usage_limit('quota exceeded'))
        self.assertTrue(ats.is_transient_rate_limit('429 too many requests'))
        self.assertTrue(ats.sandbox_blocked('sandbox: read-only'))
        self.assertFalse(ats.is_usage_exhausted('Invalid API key'))


if __name__ == '__main__':
    unittest.main()
