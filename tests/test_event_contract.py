import pathlib
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import ats
from ats_contract import accepted_event, contract, valid_phase


class ContractTests(unittest.TestCase):
    def test_live_phase_envelope_and_legacy_events(self):
        phase = {'timestamp': '2026-10-26T12:00:00Z',
                 'task_id': 'abc', 'event_type': 'phase', 'stage': 'review'}
        self.assertTrue(accepted_event(phase))
        self.assertFalse(accepted_event(phase, persisted=True))
        self.assertTrue(accepted_event(dict(phase, project='ARC'), persisted=True))
        self.assertFalse(accepted_event({'event_type': 'review_result'}))

    def test_review_and_pr_phase_vocabulary(self):
        for phase in ('implementation', 'review', 'merge', 'parallel', 'learning', 'pr_created', 'patch', 'finished'):
            self.assertTrue(valid_phase(phase))
        self.assertFalse(valid_phase('unknown-agent-phase'))

    def test_statuses_are_disjoint(self):
        data = contract()
        self.assertEqual(data['version'], 'ats.event.v1')
        self.assertFalse(set(data['active_run_stages']) & set(data['terminal_run_stages']))

    def test_run_finished_v1_interface(self):
        data = contract()
        self.assertIn('run_finished', data['event_types'])
        self.assertNotIn('finished', data['event_types'])
        completion = data['run_finished_v1']
        self.assertEqual(set(completion['required']),
                         {'approved', 'duration_seconds', 'review_rounds', 'outcome'})
        self.assertEqual(completion['outcomes'],
                         ['approved', 'escalated', 'blocked', 'error', 'interrupted'])


class RunFinishedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.app = self.root / 'app'
        self.app.mkdir()
        self.now = 100.0
        self.approved = True
        self.local_mode = False
        self.emitted = []
        self.patches = [
            patch.object(ats, 'APP', self.app),
            patch.object(ats, 'DATA', self.root / 'data'),
            patch.object(ats, 'project', return_value={'project': 'test'}),
            patch.object(ats, 'preset_task_id', return_value='run-1'),
            patch.object(ats, 'enabled_agents', return_value=['codex', 'claude']),
            patch.object(ats, 'executable', return_value='agent'),
            patch.object(ats, 'emit', side_effect=self.emitted.append),
            patch.object(ats.time, 'monotonic', side_effect=lambda: self.now),
            patch.object(ats, 'execute', side_effect=self.execute),
            patch.object(ats, 'fresh_base', return_value='origin/main'),
            patch.object(ats, 'preplan_task', return_value=None),
            patch.object(ats, 'shared_prefix', return_value=''),
            patch.object(ats, 'part_context', return_value=''),
            patch.object(ats, 'index_tree', return_value='tree'),
            patch.object(ats, 'invoke_with_fallback', return_value=('codex', 'built')),
            patch.object(ats, 'capture_learning_proposals'),
            patch.object(ats, 'review_loop', side_effect=self.review),
            patch.object(ats, 'distill_learnings'),
            patch.object(ats, 'make_local_workspace'),
            patch.object(ats, 'export_local_patch', return_value=None),
            patch.object(ats.shutil, 'which', return_value='gh'),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(ats.usage_meter.reset_context)

    def events(self, kind=None):
        events = ats.intelligence.read_events(self.root / 'data' / 'test' / 'events')
        return [e for e in events if kind is None or e['event_type'] == kind]

    def execute(self, argv, *args, **kwargs):
        self.assertEqual(self.events('run_finished'), [])
        if argv == ['git', 'rev-parse', '--show-toplevel']:
            return subprocess.CompletedProcess(argv, int(self.local_mode), str(self.app), '')
        return subprocess.CompletedProcess(argv, 0, 'head', '')

    def review(self, *args, **kwargs):
        self.assertEqual(self.events('run_finished'), [])
        ats.record('review_result', 'Review', 'run-1', approved=self.approved)
        self.now = 125.5
        if not self.approved:
            ats.record('escalated', 'Review rejected', 'run-1', blocking_reasons=['open findings'])
        # Deliberately unrelated to the persisted count: completion must count events.
        return self.approved, 'claude', 'codex', []

    def completion(self, outcome, rounds, duration):
        events = self.events('run_finished')
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertTrue(accepted_event(event, persisted=True))
        self.assertEqual(event['task_id'], 'run-1')
        self.assertEqual(event['outcome'], outcome)
        self.assertIs(event['approved'], outcome == 'approved')
        self.assertEqual(event['review_rounds'], rounds)
        self.assertEqual(event['duration_seconds'], duration)
        self.assertEqual(len([e for e in self.emitted if e['event_type'] == 'run_finished']), 1)
        self.assertNotIn('run-1', ats._run_progress)
        return event

    def test_approved_run_is_persisted_after_all_steps(self):
        def update(*args, **kwargs):
            self.assertEqual(self.events('run_finished'), [])
            self.now = 160.0
            ats.record('review_result', 'Base update review', 'run-1', approved=True)
            return True, []

        with patch.object(ats, 'update_from_base', side_effect=update):
            ats.task_flow('Fix Python test', 'codex', push=True)
        self.completion('approved', 2, 60.0)
        self.assertEqual(self.emitted[-2]['stage'], 'finished')
        self.assertEqual(self.emitted[-3]['stage'], 'pr_created')

    def test_rejected_review_and_local_early_returns(self):
        for local_mode in (False, True):
            with self.subTest(local_mode=local_mode):
                self.local_mode = local_mode
                self.approved = False
                ats.task_flow('Fix Python test', 'codex', push=False)
                self.completion('escalated', 1, 25.5)
                for path in (self.root / 'data' / 'test' / 'events').glob('*.json'):
                    path.unlink()
                self.emitted.clear()
                self.now = 100.0

    def test_approved_local_early_return(self):
        self.local_mode = True
        ats.task_flow('Fix Python test', 'codex', push=False)
        self.completion('approved', 1, 25.5)

    def test_base_update_rejection_cannot_record_success(self):
        with patch.object(ats, 'update_from_base', return_value=(False, [])):
            ats.task_flow('Fix Python test', 'codex', push=True)
        self.completion('escalated', 1, 25.5)
        finished = [e for e in self.emitted if e.get('stage') == 'finished']
        self.assertEqual([e['approved'] for e in finished], [False])

    def test_missing_gh_early_return(self):
        with patch.object(ats, 'update_from_base', return_value=(True, [])), \
             patch.object(ats.shutil, 'which', return_value=None):
            ats.task_flow('Fix Python test', 'codex', push=True)
        self.completion('approved', 1, 25.5)

    def test_base_update_error_propagates_and_is_not_approved(self):
        error = RuntimeError('update failed')
        with patch.object(ats, 'update_from_base', side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                ats.task_flow('Fix Python test', 'codex', push=True)
        self.assertIs(caught.exception, error)
        self.completion('error', 1, 25.5)
        self.assertEqual(len(self.events('error')), 1)

    def test_preflight_error_and_missing_cli_are_completed(self):
        with patch.object(ats, 'enabled_agents', return_value=[]):
            with self.assertRaisesRegex(RuntimeError, 'disabled'):
                ats.task_flow('Fix Python test', 'codex', push=False)
        self.completion('error', 0, 0)
        self.assertEqual(self.events('assigned'), [])

    def test_missing_cli_after_assignment_is_completed(self):
        with patch.object(ats, 'executable', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'No enabled agent CLI'):
                ats.task_flow('Fix Python test', 'codex', push=False)
        self.completion('error', 0, 0)
        self.assertEqual(len(self.events('assigned')), 1)

    def test_usage_block_is_completed_without_changing_propagation(self):
        with patch.object(ats, 'preplan_task', side_effect=ats.AgentUsageExhausted('codex', 'limit')):
            ats.task_flow('Fix Python test', 'codex', push=False)
        self.completion('blocked', 0, 0)
        self.assertEqual(len(self.events('blocked')), 1)

    def test_interrupt_is_completed_and_propagated(self):
        with patch.object(ats, 'preplan_task', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                ats.task_flow('Fix Python test', 'codex', push=False)
        self.completion('interrupted', 0, 0)

    def test_completion_storage_error_does_not_mask_original_error(self):
        record = ats.record

        def fail_completion(kind, *args, **kwargs):
            if kind == 'run_finished':
                raise OSError('storage unavailable')
            return record(kind, *args, **kwargs)

        with patch.object(ats, 'record', side_effect=fail_completion), \
             patch.object(ats, 'preplan_task', side_effect=RuntimeError('original failure')):
            with self.assertRaisesRegex(RuntimeError, 'original failure'):
                ats.task_flow('Fix Python test', 'codex', push=False)
        self.assertNotIn('run-1', ats._run_progress)

    def test_parallel_reviews_count_persisted_events_once(self):
        def parts(*args, **kwargs):
            def review(part):
                for cycle in range(10):
                    ats.record('review_result', 'Part review', 'run-1', subtask=part, cycle=cycle)
                    ats.record('feedback_response', 'Response', 'run-1', subtask=part, cycle=cycle)

            threads = [threading.Thread(target=review, args=(part,)) for part in (1, 2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.now = 140.0
            return True, 'claude', 'codex', []

        with patch.object(ats, 'preplan_task', return_value=['API', 'screen']), \
             patch.object(ats, 'subtask_flow', side_effect=parts):
            ats.task_flow('Fix Python test', 'codex', push=False)
        self.completion('approved', 20, 40.0)
        self.assertEqual(len(self.events('review_result')), 20)


if __name__ == '__main__':
    unittest.main()
