import contextlib
import datetime as dt
import io
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import ats
from ats_modules import run_report

SECRETS = ('SECRET-TASK', 'SECRET-OUTPUT', 'SECRET-REPAIR', 'SECRET-REVIEW', 'SECRET-FINDING',
           'SECRET-RESPONSE')


def event(kind, second, task='t1', project='demo', **fields):
    stamp = (dt.datetime(2026, 11, 1, 10, 0, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=second)).isoformat()
    fields.setdefault('summary', kind)
    return dict(timestamp=stamp, project=project, task_id=task, event_type=kind, **fields)


def parallel_run():
    """Part 1 alone (60 s), then parts 2 and 3 in one wave (40 s and 25.5 s); 100 s wall time."""
    return [
        event('assigned', 0, summary='SECRET-TASK prompt', implementer='codex', task_categories=['python']),
        event('budget', 1, subtask=1, seconds_used=0),
        event('implementation', 5, summary='SECRET-OUTPUT', implementer='codex', subtask=1),
        event('agent_usage', 6, agent='codex', purpose='implementation', subtask=1, billable_tokens=1000),
        event('pre_review_gate', 7, subtask=1, passed=False),
        event('gate_repair', 8, summary='SECRET-REPAIR', subtask=1, implementer='codex'),
        event('pre_review_gate', 9, subtask=1, passed=True),
        event('review_result', 20, summary='SECRET-REVIEW', subtask=1, cycle=0, approved=False,
              implementer='codex', reviewer='claude',
              findings=[{'id': 'F1', 'severity': 'MAJOR', 'summary': 'SECRET-FINDING'},
                        {'id': 'F2', 'severity': 'MINOR', 'summary': 'SECRET-FINDING'}]),
        event('agent_usage', 21, agent='claude', purpose='review', subtask=1, billable_tokens=500),
        event('feedback_response', 30, summary='SECRET-RESPONSE', subtask=1, cycle=0),
        event('agent_usage', 31, agent='codex', purpose='revision', subtask=1, billable_tokens=300),
        event('review_result', 50, subtask=1, cycle=1, approved=True, implementer='codex', reviewer='claude',
              findings=[{'id': 'F2', 'severity': 'SUGGESTION', 'summary': 'SECRET-FINDING'}]),
        event('subtask_completed', 61, subtask=1, implementer='codex', reviewer='claude'),
        event('budget', 62, subtask=2),
        event('budget', 62, subtask=3),
        event('implementation', 70, implementer='claude', subtask=2),
        event('implementation', 70, implementer='codex', subtask=3),
        event('review_result', 80, subtask=3, cycle=0, approved=False, implementer='codex', reviewer='claude',
              findings=[{'id': 'F1', 'severity': 'MINOR', 'summary': 'x'}]),
        event('review_result', 85, subtask=3, cycle=1, approved=True, implementer='codex', reviewer='claude',
              findings=[]),
        event('review_result', 95, subtask=2, cycle=0, approved=True, implementer='claude', reviewer='codex',
              findings=[{'id': 'F1', 'severity': 'BLOCKER', 'summary': 'x'}]),
        event('subtask_completed', 96, subtask=2, implementer='claude', reviewer='codex'),
        event('subtask_completed', 96, subtask=3, implementer='codex', reviewer='claude'),
        event('wave_timing', 97, subtasks=[2, 3], seconds=[40.0, 25.5], idle_seconds=14.5),
        event('run_finished', 100, approved=True, duration_seconds=100.0, review_rounds=5, outcome='approved'),
        # The same task id in another project never leaks into this report.
        event('review_result', 50, project='other', cycle=0, approved=False,
              findings=[{'id': 'F9', 'severity': 'BLOCKER', 'summary': 'x'}]),
        event('agent_usage', 50, project='other', purpose='review', billable_tokens=99999),
    ]


class BuildTests(unittest.TestCase):
    def test_parallel_run_has_exact_values(self):
        report = run_report.build(parallel_run(), 'demo', 't1')
        self.assertEqual(report['outcome'], 'approved')
        self.assertIs(report['approved'], True)
        self.assertEqual(report['duration_seconds'], 100.0)
        self.assertEqual(report['parts'], [
            {'part': 1, 'implementer': 'codex', 'reviewers': ['claude'], 'review_rounds': 2,
             'seconds': 60.0, 'parallel': False, 'approved': True},
            {'part': 2, 'implementer': 'claude', 'reviewers': ['codex'], 'review_rounds': 1,
             'seconds': 40.0, 'parallel': True, 'approved': True},
            {'part': 3, 'implementer': 'codex', 'reviewers': ['claude'], 'review_rounds': 2,
             'seconds': 25.5, 'parallel': True, 'approved': True},
        ])
        # Part times add up to 125.5 s, but the total stays the wall time.
        self.assertLess(report['duration_seconds'], sum(p['seconds'] for p in report['parts']))
        self.assertEqual(report['review_rounds'], 5)
        self.assertEqual(report['rejected_first_reviews'], 2)
        self.assertEqual(report['findings'], {'BLOCKER': 1, 'MAJOR': 1, 'MINOR': 1, 'SUGGESTION': 1})
        self.assertEqual(report['gate'], {'runs': 2, 'failed': 1, 'repairs': 1})
        self.assertEqual(report['tokens'], {'implementation': 1000, 'review': 500, 'revision': 300})
        self.assertEqual(report['wait_seconds'], 14.5)
        self.assertEqual(report['escalation'], [])
        self.assertEqual(report['categories'], ['python'])
        self.assertEqual(report['started'], parallel_run()[0]['timestamp'])

    def test_render_has_every_section_and_no_private_text(self):
        text = run_report.render(run_report.build(parallel_run(), 'demo', 't1'))
        for expected in ('# ATS run report t1', '- Outcome: approved',
                         '- Total duration: 100.0 s (wall time; parallel parts counted once)',
                         '| 1 | codex | claude | 2 | 60.0 s | yes |',
                         '| 2 | claude | codex | 1 | 40.0 s (parallel) | yes |',
                         '| 3 | codex | claude | 2 | 25.5 s (parallel) | yes |',
                         '- Review rounds: 5', '- Rejected first reviews: 2',
                         '- Findings: BLOCKER 1, MAJOR 1, MINOR 1, SUGGESTION 1',
                         '- Pre-review gate: 2 runs, 1 failed, 1 repairs',
                         '- implementation: 1000', '- review: 500', '- revision: 300', '- Total: 1800',
                         '- Wait time in parallel waves: 14.5 s', '## Escalation', '- none'):
            self.assertIn(expected, text)
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        self.assertNotIn('99999', text)
        meta = run_report.read_meta(self.write_temp(text))
        self.assertEqual(meta['tokens'], 1800)
        self.assertEqual(meta['parts'], 3)

    def write_temp(self, text):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = pathlib.Path(folder.name) / 'r.md'
        path.write_text(text, encoding='utf-8')
        return path

    def test_escalated_unsplit_run_reports_reasons(self):
        events = [
            event('assigned', 0, implementer='codex'),
            event('implementation', 5, implementer='codex'),
            event('review_result', 20, cycle=0, approved=False, implementer='codex', reviewer='claude',
                  findings=[{'id': 'F1', 'severity': 'MAJOR', 'summary': 'x'}]),
            event('escalated', 21, summary='Not approved', blocking_reasons=['open MAJOR F1', 'y' * 500]),
            event('run_finished', 22, approved=False, duration_seconds=22.0, review_rounds=1, outcome='escalated'),
        ]
        report = run_report.build(events, 'demo', 't1')
        self.assertEqual(report['parts'], [{'part': None, 'implementer': 'codex', 'reviewers': ['claude'],
                                            'review_rounds': 1, 'seconds': 22.0, 'parallel': False,
                                            'approved': False}])
        self.assertEqual(report['rejected_first_reviews'], 1)
        self.assertEqual(report['escalation'], ['open BLOCKER/MAJOR findings', run_report.OTHER_REASON])
        text = run_report.render(report)
        self.assertIn('| task | codex | claude | 1 | 22.0 s | no |', text)
        self.assertIn('- open BLOCKER/MAJOR findings', text)
        self.assertIn('- Wait time in parallel waves: unknown', text)

    def test_blocked_and_error_runs_use_safe_labels(self):
        for outcome, fields, expected in (
                ('blocked', {'resets': {}}, run_report.USAGE_LIMIT),
                ('blocked', {}, run_report.OTHER_REASON),
                ('error', {}, run_report.RUN_ERROR)):
            with self.subTest(outcome=outcome):
                events = [event(outcome, 1, summary='SECRET-OUTPUT CLI exception', **fields),
                          event('run_finished', 2, approved=False, duration_seconds=2, review_rounds=0,
                                outcome=outcome)]
                report = run_report.build(events, 'demo', 't1')
                self.assertEqual(report['escalation'], [expected])
                self.assertNotIn('SECRET-OUTPUT', run_report.render(report))

    def test_escalation_reasons_never_copy_cli_or_check_output(self):
        reasons = ['test command cannot start on this computer: SECRET-CHECK',
                   'review failed: SECRET-CLI', 'merge of part 2: SECRET-CLI',
                   'update with origin/SECRET-BRANCH: SECRET-CLI',
                   'open MAJOR SECRET-FINDING', 'SECRET-UNKNOWN',
                   'security pass: failing checks: SECRET-CHECK']
        events = [event('escalated', 1, summary='SECRET-SUMMARY', blocking_reasons=reasons),
                  event('run_finished', 2, outcome='escalated', review_rounds=0)]
        report = run_report.build(events, 'demo', 't1')
        self.assertEqual(report['escalation'], [
            'test command cannot start on this computer', run_report.OTHER_REASON,
            'merge of part 2 could not be completed',
            'update with the base branch could not be completed',
            'open BLOCKER/MAJOR findings', 'security pass not approved'])
        self.assertNotIn('SECRET-', str(report))
        text = run_report.render(report)
        self.assertNotIn('SECRET-', text)
        self.assertNotIn('SECRET-', run_report.table([run_report.read_meta(self.write_temp(text))], 7))

    def test_incomplete_historical_events_are_unknown(self):
        events = [event('assigned', 0, implementer='codex'),
                  event('review_result', 10, approved=False, reviewer='claude', findings='not a list')]
        report = run_report.build(events, 'demo', 't1')
        self.assertEqual(report['outcome'], 'unknown')
        self.assertIsNone(report['approved'])
        self.assertIsNone(report['duration_seconds'])
        self.assertIsNone(report['rejected_first_reviews'])
        self.assertIsNone(report['tokens'])
        self.assertIsNone(report['wait_seconds'])
        self.assertEqual(report['review_rounds'], 1)
        text = run_report.render(report)
        self.assertIn('- Total duration: unknown', text)
        self.assertIn('- Rejected first reviews: unknown', text)
        self.assertIn('- Tokens: unknown', text)
        self.assertIn('| task | codex | claude | 1 | unknown | no |', text)

    def test_empty_events(self):
        report = run_report.build([], 'demo', 't1')
        self.assertEqual(report['parts'], [])
        self.assertIsNone(report['findings'])
        self.assertIsNone(report['rejected_first_reviews'])
        text = run_report.render(report)
        self.assertIn('| unknown | | | | | |', text)
        self.assertIn('- Findings: unknown', text)
        self.assertIn('- Review rounds: unknown', text)

    def test_mixed_historical_measurements_are_unknown(self):
        events = [event('review_result', 1, cycle=0, approved=False,
                        findings=[{'id': 'F1', 'severity': 'MAJOR'}]),
                  event('review_result', 2, approved=False),
                  event('wave_timing', 3, subtasks=[1, 2], seconds=[3, 2], idle_seconds=1),
                  event('wave_timing', 4, subtasks=[3, 4]),
                  event('pre_review_gate', 5, passed=False),
                  event('pre_review_gate', 6),
                  event('agent_usage', 7, purpose='review', billable_tokens=100),
                  event('agent_usage', 8, purpose='review'),
                  event('agent_usage', 9, purpose='implementation', billable_tokens=200),
                  event('run_finished', 10, outcome='escalated', review_rounds=2)]
        report = run_report.build(events, 'demo', 't1')
        self.assertIsNone(report['rejected_first_reviews'])
        self.assertIsNone(report['findings'])
        self.assertIsNone(report['wait_seconds'])
        self.assertIsNone(report['gate']['failed'])
        self.assertEqual(report['tokens'], {'review': None, 'implementation': 200})
        self.assertEqual([(p['part'], p['seconds'], p['parallel']) for p in report['parts']],
                         [(1, 3, True), (2, 2, True), (3, None, True), (4, None, True)])
        text = run_report.render(report)
        for expected in ('- Findings: unknown', '- Rejected first reviews: unknown',
                         '- Wait time in parallel waves: unknown', '- review: unknown', '- Total: unknown',
                         '- Pre-review gate: 2 runs, unknown failed, 0 repairs'):
            self.assertIn(expected, text)
        meta = run_report.read_meta(self.write_temp(text))
        for key in ('tokens', 'findings', 'rejected_first_reviews'):
            self.assertIsNone(meta[key])
        self.assertIn('| unknown | unknown | unknown |', run_report.table([meta], 7))

    def test_missing_findings_do_not_mean_zero(self):
        for fields in ({}, {'findings': None}, {'findings': 'invalid'},
                       {'findings': [{'id': 'F1'}]}):
            with self.subTest(fields=fields):
                report = run_report.build([event('review_result', 1, cycle=0, approved=True, **fields)],
                                          'demo', 't1')
                self.assertIsNone(report['findings'])
        measured = run_report.build([event('review_result', 1, cycle=0, approved=True, findings=[])],
                                    'demo', 't1')
        self.assertEqual(measured['findings'], dict.fromkeys(run_report.SEVERITIES, 0))
        self.assertEqual(measured['rejected_first_reviews'], 0)
        # Without cycle data, IDs cannot safely be attributed to review sessions either.
        historical = run_report.build([event('review_result', 1, findings=[], approved=True)], 'demo', 't1')
        self.assertIsNone(historical['findings'])
        self.assertIsNone(historical['rejected_first_reviews'])

    def test_missing_review_events_do_not_produce_partial_counts(self):
        events = [event('review_result', 1, cycle=0, approved=False,
                        findings=[{'id': 'F1', 'severity': 'MAJOR'}]),
                  event('run_finished', 2, review_rounds=3, outcome='escalated')]
        report = run_report.build(events, 'demo', 't1')
        self.assertEqual(report['review_rounds'], 3)
        self.assertIsNone(report['findings'])
        self.assertIsNone(report['rejected_first_reviews'])

    def test_missing_initial_review_rounds_are_unknown(self):
        # Only the approving cycle 1 survived; the rejected cycle 0 and its findings are missing.
        for finished in ([], [event('run_finished', 3, review_rounds=1, outcome='approved')]):
            with self.subTest(run_finished=bool(finished)):
                events = [event('review_result', 2, subtask=1, cycle=1, approved=True, findings=[])] + finished
                report = run_report.build(events, 'demo', 't1')
                self.assertIsNone(report['rejected_first_reviews'])
                self.assertIsNone(report['findings'])
        # A gap inside a later review loop (cycle 0 of the integration review missing) is unknown too.
        events = [event('review_result', 1, subtask=2, cycle=0, approved=True, findings=[]),
                  event('merge_conflict', 2, subtask=2),
                  event('review_result', 3, subtask=2, cycle=1, approved=True, findings=[]),
                  event('run_finished', 4, review_rounds=2, outcome='approved')]
        report = run_report.build(events, 'demo', 't1')
        self.assertIsNone(report['rejected_first_reviews'])
        self.assertIsNone(report['findings'])
        text = run_report.render(report)
        self.assertIn('- Rejected first reviews: unknown', text)
        self.assertIn('- Findings: unknown', text)

    def test_unavailable_usage_from_the_producer_is_unknown(self):
        stored = []

        def keep(kind, summary, task=None, **fields):
            stored.append(event(kind, len(stored), task=task, summary=summary, **fields))

        ats.usage_meter.set_context(task_id='t1')
        self.addCleanup(ats.usage_meter.reset_context)
        with patch.object(ats, 'record', side_effect=keep):
            ats.note_usage('codex', 'review', {'usage': ats.usage_meter.usage()})
            ats.note_usage('codex', 'implementation', {'usage': ats.usage_meter.usage(100, 20, 50, None)})
        self.assertEqual([e['billable_tokens'] for e in stored], [0, 125])
        stored.append(event('run_finished', 5, review_rounds=0, outcome='approved'))
        report = run_report.build(stored, 'demo', 't1')
        self.assertEqual(report['tokens'], {'review': None, 'implementation': 125})
        text = run_report.render(report)
        self.assertIn('- review: unknown', text)
        self.assertIn('- Total: unknown', text)
        meta = run_report.read_meta(self.write_temp(text))
        self.assertIsNone(meta['tokens'])
        self.assertTrue(run_report.table([meta], 7).splitlines()[-1].endswith('| unknown |'))
        # Older events with only billable_tokens keep their measured value.
        older = run_report.build([event('agent_usage', 1, purpose='review', billable_tokens=0)], 'demo', 't1')
        self.assertEqual(older['tokens'], {'review': 0})

    def test_part_duration_needs_its_start_and_end(self):
        cases = (
            # No start event: the interval would begin after the implementation.
            ([event('implementation', 5, subtask=1), event('subtask_completed', 20, subtask=1)], None),
            # Interrupted: no end event for the part, only a later run error.
            ([event('budget', 0, subtask=1), event('implementation', 5, subtask=1),
              event('error', 20)], None),
            # Interrupted during the review: the last stored review is not the end of the part.
            ([event('budget', 0, subtask=1), event('implementation', 5, subtask=1),
              event('review_result', 12, subtask=1, cycle=0, approved=False, findings=[])], None),
            ([event('budget', 0, subtask=1), event('implementation', 5, subtask=1),
              event('escalated', 20, subtask=1, blocking_reasons=['budget: time'])], 20.0),
            ([event('budget', 0, subtask=1), event('blocked', 9, subtask=1, resets={})], 9.0),
        )
        for events, seconds in cases:
            with self.subTest(events=[e['event_type'] for e in events]):
                report = run_report.build(events, 'demo', 't1')
                self.assertEqual(report['parts'][0]['seconds'], seconds)
                row = next(line for line in run_report.render(report).splitlines() if line.startswith('| 1 |'))
                self.assertEqual(row.split(' | ')[4], run_report._show(seconds, ' s'))

    def test_sequential_resume_reports_reused_parts_as_approved_with_their_agents(self):
        # subtask_flow skips a checkpointed wave: subtask_resumed has no agents, the summary has them.
        parts = [{'subtask': 1, 'implementer': 'codex', 'reviewer': 'claude'},
                 {'subtask': 2, 'implementer': 'claude', 'reviewer': 'codex'}]
        events = [event('assigned', 0, implementer='codex'),
                  event('subtask_resumed', 1, subtask=1),
                  event('budget', 2, subtask=2),
                  event('implementation', 5, subtask=2, implementer='claude'),
                  event('review_result', 9, subtask=2, cycle=0, approved=True, implementer='claude',
                        reviewer='codex', findings=[]),
                  event('subtask_completed', 10, subtask=2, implementer='claude', reviewer='codex'),
                  event('subtasks_completed', 11, parts=parts),
                  event('run_finished', 12, approved=True, duration_seconds=12.0, review_rounds=1,
                        outcome='approved')]
        report = run_report.build(events, 'demo', 't1')
        self.assertEqual(report['parts'][0], {'part': 1, 'implementer': 'codex', 'reviewers': ['claude'],
                                              'review_rounds': 0, 'seconds': None, 'parallel': False,
                                              'approved': True})
        self.assertEqual(report['parts'][1]['approved'], True)
        self.assertIn('| 1 | codex | claude | 0 | unknown | yes |', run_report.render(report))
        # Escalated later, so no summary: the reused part is still approved, its agents unknown.
        events = [e for e in events if e['event_type'] not in ('subtasks_completed', 'run_finished')]
        report = run_report.build(events, 'demo', 't1')
        self.assertEqual(report['parts'][0]['approved'], True)
        self.assertIsNone(report['parts'][0]['implementer'])
        self.assertIn('| 1 | unknown | unknown | 0 | unknown | yes |', run_report.render(report))

    def test_reused_parallel_node_reports_approval_and_agents(self):
        # The DAG path records who built and reviewed the reused node on subtask_resumed itself.
        events = [event('subtask_resumed', 1, subtask=2, implementer='claude', reviewer='codex'),
                  event('budget', 1, subtask=3),
                  event('implementation', 4, subtask=3, implementer='codex'),
                  event('review_result', 8, subtask=3, cycle=0, approved=True, implementer='codex',
                        reviewer='claude', findings=[]),
                  event('subtask_completed', 9, subtask=3, implementer='codex', reviewer='claude'),
                  event('escalated', 20, blocking_reasons=['merge of part 3: conflict'])]
        report = run_report.build(events, 'demo', 't1')
        self.assertEqual(report['parts'][0], {'part': 2, 'implementer': 'claude', 'reviewers': ['codex'],
                                              'review_rounds': 0, 'seconds': None, 'parallel': False,
                                              'approved': True})
        self.assertIn('| 2 | claude | codex | 0 | unknown | yes |', run_report.render(report))

    def test_finding_ids_are_scoped_to_integration_and_base_update_reviews(self):
        # Both producers start a fresh review_loop at cycle 0, reusing F1 after initial approval.
        for number, terminal in ((2, 'subtask_merged'), (None, 'base_merged')):
            with self.subTest(part=number):
                events = [event('review_result', 1, subtask=number, cycle=0, approved=False,
                                findings=[{'id': 'F1', 'severity': 'BLOCKER'}]),
                          event('review_result', 2, subtask=number, cycle=1, approved=True,
                                findings=[{'id': 'F1', 'severity': 'MAJOR'}]),
                          event('merge_conflict', 3, subtask=number),
                          event('review_result', 4, subtask=number, cycle=0, approved=False,
                                findings=[{'id': 'F1', 'severity': 'MINOR'}]),
                          event('review_result', 5, subtask=number, cycle=1, approved=True, findings=[]),
                          event(terminal, 6, subtask=number),
                          event('run_finished', 7, review_rounds=4, outcome='approved')]
                report = run_report.build(events, 'demo', 't1')
                self.assertEqual(report['findings'], {'BLOCKER': 0, 'MAJOR': 1, 'MINOR': 1, 'SUGGESTION': 0})
                self.assertEqual(report['review_rounds'], 4)
                self.assertEqual(report['rejected_first_reviews'], 2)
                # Even without the older merge marker, cycle 0 identifies the new session.
                events = [e for e in events if e['event_type'] != 'merge_conflict']
                self.assertEqual(run_report.build(events, 'demo', 't1')['findings'], report['findings'])

    def test_parallel_build_and_merge_repair_have_separate_durations(self):
        for terminal in ('subtask_merged', 'escalated', 'error'):
            with self.subTest(terminal=terminal):
                events = [event('budget', 0, subtask=2),
                          event('implementation', 1, subtask=2, implementer='codex'),
                          event('subtask_completed', 3, subtask=2, implementer='codex'),
                          # The other part keeps this part waiting 27 s; do not add that to its repair.
                          event('wave_timing', 30, subtasks=[1, 2], seconds=[30, 3], idle_seconds=27),
                          event('merge_conflict', 31, subtask=2, implementer='codex'),
                          event('implementation', 35, subtask=2, implementer='claude'),
                          event('review_result', 47, subtask=2, reviewer='codex', findings=[], cycle=0),
                          event(terminal, 48, subtask=2),
                          event('run_finished', 50, duration_seconds=50, review_rounds=1,
                                outcome='approved' if terminal == 'subtask_merged' else 'escalated')]
                report = run_report.build(events, 'demo', 't1')
                self.assertEqual(report['duration_seconds'], 50)
                self.assertEqual(report['parts'][1]['seconds'], 3)
                self.assertEqual(report['wait_seconds'], 27)
                self.assertEqual(report['integrations'], [
                    {'part': 2, 'seconds': 17.0, 'implementers': ['claude'], 'reviewers': ['codex'],
                     'approved': terminal == 'subtask_merged'}])
                text = run_report.render(report)
                self.assertIn('## Integration', text)
                self.assertIn('| 2 | claude | codex | 17.0 s |', text)

    def test_unmeasured_or_unfinished_merge_repair_is_unknown(self):
        for fields in ({'timestamp': 'invalid'}, {}):
            with self.subTest(fields=fields):
                start = event('merge_conflict', 3, subtask=2, implementer='codex')
                start.update(fields)
                events = [event('wave_timing', 2, subtasks=[1, 2], seconds=[2, 1], idle_seconds=1), start,
                          event('implementation', 5, subtask=2, implementer='codex')]
                if fields:
                    events.append(event('subtask_merged', 20, subtask=2))
                report = run_report.build(events, 'demo', 't1')
                self.assertIsNone(report['integrations'][0]['seconds'])
                self.assertIn('unknown', run_report.render(report).split('## Integration')[1])

    def test_marker_survives_hostile_values(self):
        events = [event('run_finished', 1, task='a-->b', approved=False, duration_seconds=1,
                        review_rounds=0, outcome='error')]
        text = run_report.render(run_report.build(events, 'demo', 'a-->b'))
        self.assertEqual(text.split('\n', 1)[0].count('-->'), 1)
        self.assertEqual(run_report.read_meta(self.write_temp(text))['task_id'], 'a-->b')


class StorageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data = pathlib.Path(folder.name)

    def save(self, project, task, finished):
        events = [dict(timestamp=finished, project=project, task_id=task, event_type='run_finished',
                       approved=True, duration_seconds=12.5, review_rounds=2, outcome='approved')]
        return run_report.write(self.data, project, task, events)

    def test_write_stores_under_project_reports(self):
        path = self.save('demo', 't1', '2026-11-01T10:00:00+00:00')
        self.assertEqual(path, self.data / 'demo' / 'reports' / 't1.md')
        self.assertIn('# ATS run report t1', path.read_text(encoding='utf-8'))
        unsafe = self.save('demo', '../../escape', '2026-11-01T10:00:00+00:00')
        self.assertEqual(unsafe.parent, self.data / 'demo' / 'reports')

    def test_recent_respects_date_bounds_and_project(self):
        now = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc)
        self.save('demo', 'edge', '2026-11-01T12:00:00+00:00')       # exactly 7 days: included
        self.save('demo', 'old', '2026-11-01T11:59:59+00:00')        # just outside
        self.save('demo', 'new', '2026-11-08T11:00:00Z')
        self.save('demo', 'future', '2026-11-09T12:00:00+00:00')
        self.save('other', 'foreign', '2026-11-08T11:00:00+00:00')
        (self.data / 'demo' / 'reports' / 'broken.md').write_text('# no marker\n', encoding='utf-8')
        rows = run_report.recent(self.data, 'demo', 7, now=now)
        self.assertEqual([r['task_id'] for r in rows], ['new', 'edge'])
        self.assertEqual(run_report.recent(self.data, 'missing', 7, now=now), [])

    def test_table(self):
        self.assertEqual(run_report.table([], 7), 'No run reports in the last 7 days.')
        self.save('demo', 'abcdef123456', '2026-11-08T11:00:00+00:00')
        rows = run_report.recent(self.data, 'demo', 7, now=dt.datetime(2026, 11, 8, 12, tzinfo=dt.timezone.utc))
        lines = run_report.table(rows, 7).splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[2], '| 2026-11-08 11:00 | abcdef12 | approved | 12.5 s | 0 | 2 | unknown | '
                                   'unknown | unknown |')


class CommandTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data = pathlib.Path(folder.name)
        for item in (patch.object(ats, 'DATA', self.data),
                     patch.object(ats, 'project', return_value={'project': 'demo'}),
                     patch.dict(os.environ, {'ATS_NO_SELF_UPDATE': '1'})):
            item.start()
            self.addCleanup(item.stop)

    def run_cli(self, *argv):
        out = io.StringIO()
        with patch.object(ats.sys, 'argv', ['ats.py', *argv]), contextlib.redirect_stdout(out):
            self.assertEqual(ats.main(), 0)
        return out.getvalue()

    def test_report_command_prints_recent_reports(self):
        recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=9)).isoformat()
        for task, finished in (('recent-run', recent), ('old-run', old)):
            run_report.write(self.data, 'demo', task, [dict(
                timestamp=finished, project='demo', task_id=task, event_type='run_finished', approved=False,
                duration_seconds=3, review_rounds=1, outcome='escalated')])
        output = self.run_cli('report', '--days', '7')
        self.assertIn('| Finished (UTC) | Task | Outcome |', output)
        self.assertIn('| recent-r | escalated | 3 s |', output)
        self.assertNotIn('old-run', output)
        self.assertIn('old-run', self.run_cli('report', '--days', '10'))

    def test_report_command_without_reports_and_invalid_days(self):
        self.assertIn('No run reports in the last 7 days.', self.run_cli('report'))
        for days in ('0', '-1', 'week'):
            with self.subTest(days=days), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.run_cli('report', '--days', days)


class TaskFlowReportTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = pathlib.Path(folder.name)
        self.app = self.root / 'app'
        self.app.mkdir()
        self.reports = self.root / 'data' / 'test' / 'reports'
        for item in (
            patch.object(ats, 'APP', self.app),
            patch.object(ats, 'DATA', self.root / 'data'),
            patch.object(ats, 'project', return_value={'project': 'test'}),
            patch.object(ats, 'preset_task_id', return_value='run-1'),
            patch.object(ats, 'enabled_agents', return_value=['codex', 'claude']),
            patch.object(ats, 'executable', return_value='agent'),
            patch.object(ats, 'emit'),
            patch.object(ats, 'execute', side_effect=self.execute),
            patch.object(ats, 'fresh_base', return_value='origin/main'),
            patch.object(ats, 'preplan_task', return_value=None),
            patch.object(ats, 'shared_prefix', return_value=''),
            patch.object(ats, 'part_context', return_value=''),
            patch.object(ats, 'index_tree', return_value='tree'),
            patch.object(ats, 'invoke_with_fallback', return_value=('codex', 'SECRET-OUTPUT')),
            patch.object(ats, 'capture_learning_proposals'),
            patch.object(ats, 'review_loop', side_effect=self.review),
            patch.object(ats, 'distill_learnings'),
            patch.object(ats, 'make_local_workspace'),
            patch.object(ats, 'export_local_patch', return_value=None),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(ats.usage_meter.reset_context)

    def execute(self, argv, *args, **kwargs):
        if argv == ['git', 'rev-parse', '--show-toplevel']:
            return subprocess.CompletedProcess(argv, 1, '', '')   # local mode: no PR publishing
        return subprocess.CompletedProcess(argv, 0, 'head', '')

    def review(self, *args, **kwargs):
        self.assertFalse(self.reports.exists())   # never before run_finished
        ats.record('review_result', 'SECRET-REVIEW', 'run-1', cycle=0, approved=True, reviewer='claude',
                   implementer='codex', findings=[])
        return True, 'claude', 'codex', []

    def test_report_is_written_after_run_finished(self):
        with contextlib.redirect_stdout(io.StringIO()):
            # 'secret' in the task text makes it high risk; approve so the run reaches the report
            ats.task_flow('SECRET-TASK description', 'codex', push=False, approved_risk=True)
        text = (self.reports / 'run-1.md').read_text(encoding='utf-8')
        self.assertIn('- Outcome: approved', text)
        self.assertIn('- Review rounds: 1', text)
        self.assertIn('| task | codex | claude | 1 |', text)
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def test_report_failure_does_not_mask_the_outcome(self):
        stderr = io.StringIO()
        with patch.object(run_report, 'write', side_effect=OSError('disk full')), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            ats.task_flow('Fix Python test', 'codex', push=False)
        self.assertIn('could not write the run report', stderr.getvalue())
        finished = [e for e in ats.intelligence.read_events(self.root / 'data' / 'test' / 'events')
                    if e['event_type'] == 'run_finished']
        self.assertEqual([e['outcome'] for e in finished], ['approved'])

    def test_report_failure_keeps_the_original_error(self):
        with patch.object(run_report, 'write', side_effect=OSError('disk full')), \
             patch.object(ats, 'preplan_task', side_effect=RuntimeError('original failure')), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'original failure'):
                ats.task_flow('Fix Python test', 'codex', push=False)

    def test_failed_run_report_records_error(self):
        with patch.object(ats, 'preplan_task', side_effect=RuntimeError('original failure')), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError):
                ats.task_flow('Fix Python test', 'codex', push=False)
        text = (self.reports / 'run-1.md').read_text(encoding='utf-8')
        self.assertIn('- Outcome: error', text)
        self.assertIn('- run error (details in the event log)', text)
        self.assertNotIn('original failure', text)
        self.assertEqual(run_report.read_meta(self.reports / 'run-1.md')['outcome'], 'error')


if __name__ == '__main__':
    unittest.main()
