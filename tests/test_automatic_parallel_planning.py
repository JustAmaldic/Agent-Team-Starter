import pathlib
import tempfile
import unittest
from unittest.mock import patch

import ats


ARC_SECURITY_TASK = (
    "Make functions so ARC automatically scans the inbox for spoofed senders, urgent payment "
    "requests and odd links. Each flagged mail gets a risk score and a one-line reason. "
    "Nothing is deleted without asking. Warn when a security or password-reset mail arrives "
    "that you did not trigger. Plan: scan_inbox gets a 'security' category for new-login "
    "and password-reset mails from Google, GitHub, Cloudflare, Anthropic, Bitvavo and IBKR; "
    "anything you did not do yourself goes to the top of the morning brief. "
    "Review newsletters and sign-up mails once a week and list which services hold your data."
)


class PreflightPlanningTests(unittest.TestCase):
    def test_arc_security_task_triggers_automatic_planning(self):
        self.assertTrue(ats.should_preplan(ARC_SECURITY_TASK))
        self.assertTrue(ats.should_preplan("Fix CSS", "always"))
        self.assertFalse(ats.should_preplan(ARC_SECURITY_TASK, "never"))
        self.assertFalse(ats.should_preplan("Fix CSS", "auto"))

    def test_valid_parallel_plan_runs_in_independent_waves(self):
        plan = [
            "Inspect inbox interfaces and add shared event contract tests",
            [
                "Implement spoofing and suspicious links scorer only in inbox_security.py with tests",
                "Implement newsletter signup inventory only in privacy_audit.py with tests",
            ],
            "Integrate security mail warnings with morning briefing and test review gates",
        ]
        self.assertTrue(ats.valid_plan(plan))
        self.assertEqual([len(w) for w in ats.plan_waves(plan)], [1, 2, 1])

    def test_preflight_produces_plan_before_implementation(self):
        output = ('ATS_SUBTASKS_JSON: ["Inspect shared interfaces", '
                  '["Implement spoof detector in detector.py", '
                  '"Implement newsletter audit in newsletter.py"], '
                  '"Integrate and test"]')
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(ats, 'enabled_agents', return_value=['codex', 'claude']), \
                 patch.object(ats, 'phase'), patch.object(ats, 'index_tree', return_value='tree0'), \
                 patch.object(ats, 'restore_if_changed', return_value=[]), \
                 patch.object(ats, 'record') as record, \
                 patch.object(ats, 'invoke_with_fallback', return_value=('codex', output)) as agent:
                plan = ats.preplan_task(ARC_SECURITY_TASK, 'task1', pathlib.Path(tmp),
                                        'codex', set(), {}, mode='auto')
        self.assertEqual(len(ats.plan_waves(plan)[1]), 2)
        self.assertEqual(agent.call_count, 1)
        self.assertIn('Plan ONLY, do not edit any files', agent.call_args.args[1])
        self.assertIn('parallel_pairs', record.call_args.kwargs)

    def test_missing_plan_fails_without_running_implementation(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(ats, 'enabled_agents', return_value=['codex', 'claude']), \
                 patch.object(ats, 'phase'), patch.object(ats, 'index_tree', return_value='tree0'), \
                 patch.object(ats, 'restore_if_changed', return_value=[]), \
                 patch.object(ats, 'invoke_with_fallback', return_value=('codex', 'No split needed')) as invoke:
                with self.assertRaisesRegex(RuntimeError, 'No implementation started'):
                    ats.preplan_task(ARC_SECURITY_TASK, 'task1', pathlib.Path(tmp),
                                     'codex', set(), {}, mode='auto')
                self.assertEqual(invoke.call_count, 2)

    def test_always_mode_needs_parallel_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(ats, 'enabled_agents', return_value=['codex', 'claude']), \
                 patch.object(ats, 'phase'), patch.object(ats, 'index_tree', return_value='tree0'), \
                 patch.object(ats, 'restore_if_changed', return_value=[]), \
                 patch.object(ats, 'invoke_with_fallback',
                                   return_value=('codex', 'ATS_SUBTASKS_JSON: ["Part A", "Part B"]')):
                with self.assertRaisesRegex(RuntimeError, 'No safe parallel pair'):
                    ats.preplan_task(ARC_SECURITY_TASK, 'task1', pathlib.Path(tmp),
                                     'codex', set(), {}, mode='always')


if __name__ == '__main__':
    unittest.main()
