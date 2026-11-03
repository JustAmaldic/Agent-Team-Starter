"""t53: plans with fewer, larger parts. t54: one Codex preflight per worktree per run."""
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import ats

TASK = ('Add a weekly privacy report to ARC: list which services hold the user data, show it in the '
        'morning brief, add a settings switch and tests for the report, the brief and the switch, and '
        'also document the report in the README so the owner knows how it works and when it runs.')


def dag(count):
    return 'ATS_DAG_JSON: ' + json.dumps({'plan_version': 2, 'tasks': [
        {'id': f'p{i}', 'title': f'Implement bounded part number {i} with tests', 'depends_on': [],
         'owned_paths': [f'part{i}.py']} for i in range(count)]})


class FewerPartsTests(unittest.TestCase):
    def plan(self, outputs, conf=None):
        replies = list(outputs)
        prompts, events = [], []

        def agent(name, prompt, *args, **kwargs):
            prompts.append(prompt)
            return name, replies.pop(0)

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(ats, 'enabled_agents', return_value=['codex', 'claude']), \
             patch.object(ats, 'phase'), patch.object(ats, 'index_tree', return_value='tree0'), \
             patch.object(ats, 'restore_if_changed', return_value=[]), \
             patch.object(ats, 'shared_prefix', return_value=''), \
             patch.object(ats, 'record', side_effect=lambda kind, *a, **f: events.append((kind, f))), \
             patch.object(ats, 'invoke_with_fallback', side_effect=agent), patch('builtins.print'):
            plan = ats.preplan_task(TASK, 't1', pathlib.Path(tmp), 'codex', set(), conf or {}, mode='auto')
        return plan, prompts, events

    def test_prompt_asks_for_few_parts_and_real_boundaries(self):
        _, prompts, _ = self.plan([dag(3)])
        self.assertIn('as FEW concrete implementable parts as possible', prompts[0])
        self.assertIn('usually 2-5', prompts[0])
        self.assertIn('Split only at real boundaries', prompts[0])
        self.assertIn('Never make a part only for tests', prompts[0])

    def test_long_plan_is_asked_once_to_merge(self):
        plan, prompts, events = self.plan([dag(9), dag(4)])
        self.assertEqual(len(plan['tasks']), 4)
        self.assertEqual(len(prompts), 2)
        self.assertIn('The plan has 9 parts; merge related steps into at most 5 parts', prompts[1])

    def test_still_long_plan_runs_with_a_warning(self):
        plan, _, events = self.plan([dag(9), dag(8)])
        self.assertEqual(len(plan['tasks']), 8)
        warning = [f for kind, f in events if kind == 'planning_warning']
        self.assertEqual(warning[0]['parts'], 8)

    def test_project_setting(self):
        self.assertEqual(ats.max_plan_parts({'max_plan_parts': 8}), 8)
        self.assertEqual(ats.max_plan_parts({'max_plan_parts': 99}), ats.MAX_PARTS)
        self.assertEqual(ats.max_plan_parts({'max_plan_parts': 'x'}), ats.DEFAULT_MAX_PLAN_PARTS)
        _, prompts, _ = self.plan([dag(7)], conf={'max_plan_parts': 7})
        self.assertEqual(len(prompts), 1)


class SinglePreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self.temp.name)
        self.saved = set(ats._preflight_passed)
        ats._preflight_passed.clear()

    def tearDown(self):
        ats._preflight_passed.clear()
        ats._preflight_passed.update(self.saved)
        self.temp.cleanup()

    def call(self, invoke, purpose='implementation', cwd=None):
        with patch.object(ats, 'invoke', side_effect=invoke), patch.object(ats, 'record'), \
             patch.object(ats, 'enabled_agents', return_value=['codex', 'claude']), \
             patch.object(ats, 'executable', return_value='x'), \
             patch.object(ats, 'codex_preflight') as preflight, patch('builtins.print'), patch('sys.stderr'):
            try:
                ats.invoke_with_fallback('codex', 'p', cwd or self.folder, ats.UnavailableAgents(), 't', purpose,
                                         quiet=True, fallback=False)
            except RuntimeError:
                pass
        return preflight.call_count

    def test_one_preflight_per_worktree(self):
        ok = lambda *a, **k: 'done'
        self.assertEqual(self.call(ok), 1)
        self.assertEqual(self.call(ok), 0)                     # same worktree: no second probe
        self.assertEqual(self.call(ok, purpose='revision'), 0)
        side = self.folder / 'side'
        side.mkdir()
        self.assertEqual(self.call(ok, cwd=side), 1)           # a parallel part's own worktree

    def test_sandbox_block_checks_again(self):
        def blocked(*a, **k):
            raise ats.AgentSandboxBlocked('blocked')
        self.assertEqual(self.call(lambda *a, **k: 'done'), 1)
        self.call(blocked)
        self.assertEqual(self.call(lambda *a, **k: 'done'), 1)


if __name__ == '__main__':
    unittest.main()
