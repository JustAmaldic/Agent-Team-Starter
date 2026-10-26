import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ats


class AgentConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Path(self.temp.name)
        self.original_app = ats.APP
        ats.APP = self.app
        self.addCleanup(setattr, ats, 'APP', self.original_app)

    def test_default_agents_preserve_existing_behavior(self):
        self.assertEqual(ats.enabled_agents(), ['codex', 'claude'])

    def test_enable_and_disable_agents(self):
        self.assertEqual(ats.configure_agents(enable=['gemini'], disable=['claude']),
                         ['codex', 'gemini'])
        self.assertEqual(ats.enabled_agents(), ['codex', 'gemini'])
        self.assertEqual(json.loads((self.app / '.ats-agents.json').read_text()),
                         {'enabled': ['codex', 'gemini']})

    def test_cannot_disable_last_agent(self):
        ats.configure_agents(disable=['claude'])
        with self.assertRaisesRegex(RuntimeError, 'At least one'):
            ats.configure_agents(disable=['codex'])
        self.assertEqual(ats.enabled_agents(), ['codex'])

    def test_rejects_invalid_config(self):
        (self.app / '.ats-agents.json').write_text('{"enabled": ["unknown"]}')
        with self.assertRaisesRegex(RuntimeError, 'enabled must'):
            ats.enabled_agents()

    def test_rejects_duplicate_config_entries(self):
        (self.app / '.ats-agents.json').write_text('{"enabled": ["codex", "codex"]}')
        with self.assertRaisesRegex(RuntimeError, 'enabled must'):
            ats.enabled_agents()

    def test_alternate_skips_disabled_and_unavailable(self):
        ats.configure_agents(enable=['gemini'], disable=['claude'])
        self.assertEqual(ats.alternate_agent('codex'), 'gemini')
        self.assertIsNone(ats.alternate_agent('codex', {'gemini'}))

    def test_disabled_agent_cannot_be_run(self):
        ats.configure_agents(disable=['claude'])
        with self.assertRaisesRegex(RuntimeError, 'disabled'):
            ats.invoke_with_fallback('claude', 'prompt', self.app,
                                     ats.UnavailableAgents(), 'task', 'implementation')

    def test_missing_cli_falls_back_only_to_enabled_agents(self):
        ats.configure_agents(enable=['gemini'], disable=['claude'])
        with patch.object(ats, 'executable', side_effect=lambda name: name == 'gemini'), \
             patch.object(ats, 'invoke', return_value='ok'):
            chosen, output = ats.invoke_with_fallback(
                'codex', 'prompt', self.app, ats.UnavailableAgents(), 'task', 'review',
                quiet=True)
        self.assertEqual((chosen, output), ('gemini', 'ok'))


if __name__ == '__main__':
    unittest.main()
