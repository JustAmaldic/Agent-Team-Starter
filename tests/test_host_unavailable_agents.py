import os
import unittest
from unittest.mock import patch

import ats


class HostUnavailableAgentsTests(unittest.TestCase):
    def test_claude_excluded_from_routing_when_quota_exhausted(self):
        with patch.dict(os.environ, {"ATS_BLOCKED_AGENTS": "claude"}), \
             patch.object(ats, "agents_config_path") as cfg:
            cfg.return_value.exists.return_value = False
            agents = ats.enabled_agents()
            self.assertNotIn("claude", agents)
            self.assertIn("codex", agents)

    def test_reenabled_on_fresh_host_run_after_reset(self):
        with patch.object(ats, "agents_config_path") as cfg:
            cfg.return_value.exists.return_value = False
            with patch.dict(os.environ, {"ATS_BLOCKED_AGENTS": "claude"}):
                self.assertNotIn("claude", ats.enabled_agents())
            with patch.dict(os.environ, {"ATS_BLOCKED_AGENTS": ""}):
                self.assertIn("claude", ats.enabled_agents())

    def test_configured_agents_are_not_lost_when_temporarily_blocked(self):
        with patch.dict(os.environ, {"ATS_BLOCKED_AGENTS": "claude"}), \
             patch.object(ats, "agents_config_path") as cfg:
            cfg.return_value.exists.return_value = False
            self.assertIn("claude", ats.enabled_agents(include_blocked=True))
            self.assertNotIn("claude", ats.enabled_agents())

    def test_no_available_agent_has_clear_error(self):
        with patch.dict(os.environ, {"ATS_BLOCKED_AGENTS": "codex,claude"}), \
             patch.object(ats, "agents_config_path") as cfg, \
             patch.object(ats, "project", return_value={"project": "Demo"}):
            cfg.return_value.exists.return_value = False
            with self.assertRaisesRegex(RuntimeError, "No available agents"):
                ats.select("Review Python bug")

    def test_blank_and_whitespace_entries_safe(self):
        with patch.dict(os.environ, {"ATS_BLOCKED_AGENTS": " Claude, , "}), \
             patch.object(ats, "agents_config_path") as cfg:
            cfg.return_value.exists.return_value = False
            self.assertNotIn("claude", ats.enabled_agents())


if __name__ == "__main__":
    unittest.main()
