import unittest

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
        for phase in ('implementation', 'review', 'pr_created', 'patch', 'finished'):
            self.assertTrue(valid_phase(phase))
        self.assertFalse(valid_phase('unknown-agent-phase'))

    def test_statuses_are_disjoint(self):
        data = contract()
        self.assertEqual(data['version'], 'ats.event.v1')
        self.assertFalse(set(data['active_run_stages']) & set(data['terminal_run_stages']))


if __name__ == '__main__':
    unittest.main()
