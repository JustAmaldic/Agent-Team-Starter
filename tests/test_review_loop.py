"""End-to-end tests for the enforced review loop, using a real temporary Git repo."""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats

PASS = [sys.executable, '-c', 'pass']
FAIL = [sys.executable, '-c', 'import sys; sys.exit(1)']


def findings_line(*items):
    return 'ATS_FINDINGS_JSON: ' + json.dumps(list(items))


def responses_line(*items):
    return 'ATS_RESPONSES_JSON: ' + json.dumps(list(items))


MAJOR = {'id': 'F1', 'severity': 'MAJOR', 'file': 'app.py', 'line': '1', 'summary': 'No timeout'}
BLOCKER = {'id': 'B1', 'severity': 'BLOCKER', 'file': 'app.py', 'line': '1', 'summary': 'Data loss'}


class ReviewLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self.temp.name)
        git = ['git', '-c', 'user.name=t', '-c', 'user.email=t@t']
        subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=self.folder, check=True)
        (self.folder / 'app.py').write_text('x = 1\n')
        subprocess.run(['git', 'add', '-A'], cwd=self.folder, check=True)
        subprocess.run(git + ['commit', '-q', '-m', 'init'], cwd=self.folder, check=True)
        # implementation already happened:
        (self.folder / 'app.py').write_text('x = 2\n')
        self.events = []
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def run_loop(self, script, conf=None, implementer='codex', unavailable=None):
        """script: list of (expected_agent, purpose_marker, callable(folder) -> output)."""
        steps = list(script)

        def fake_invoke(agent, prompt, cwd, quiet=False):
            self.calls.append((agent, prompt))
            expected, action = steps.pop(0)
            self.assertEqual(agent, expected)
            return action(pathlib.Path(cwd), prompt)

        def fake_record(kind, summary, task=None, **fields):
            self.events.append(dict(kind=kind, summary=summary, **fields))

        config = {'test_commands': [PASS], 'max_revisions': 2}
        config.update(conf or {})
        with patch.object(ats, 'invoke', side_effect=fake_invoke), \
             patch.object(ats, 'record', side_effect=fake_record), \
             patch.object(ats, 'capture_learning_proposals'), \
             patch.object(ats, 'codex_preflight'):
            result = ats.review_loop('task', 'tid', self.folder, 'ats/b', implementer,
                                     unavailable if unavailable is not None else ats.UnavailableAgents(),
                                     config, quiet=True)
        self.assertEqual(steps, [], 'not every scripted agent call happened')
        return result

    def kinds(self):
        return [e['kind'] for e in self.events]

    # ------------------------------------------------------------------ basics
    def test_clean_approval(self):
        approved, reviewer, _, history = self.run_loop([
            ('claude', lambda f, p: 'Looks good.\n' + findings_line() + '\nVERDICT: APPROVE'),
        ])
        self.assertTrue(approved)
        self.assertEqual(reviewer, 'claude')
        self.assertEqual(len(history), 1)

    def test_markdown_verdict_is_accepted(self):
        approved, *_ = self.run_loop([
            ('claude', lambda f, p: findings_line() + '\n**VERDICT: APPROVE**'),
        ])
        self.assertTrue(approved)

    def test_echoed_prompt_does_not_count_as_verdict(self):
        # The reviewer echoes the prompt but never writes its own verdict line.
        approved, *_ = self.run_loop([
            ('claude', lambda f, p: p + '\nI ran out of time.'),
            ('codex', lambda f, p: responses_line()),
            ('claude', lambda f, p: p),
            ('codex', lambda f, p: responses_line()),
            ('claude', lambda f, p: p),
        ])
        self.assertFalse(approved)
        result = [e for e in self.events if e['kind'] == 'review_result'][0]
        self.assertIsNone(result['verdict'])
        self.assertEqual(result['findings'], [], 'echoed placeholder findings must be ignored')

    def test_last_verdict_line_wins(self):
        self.assertEqual(ats.parse_verdict('VERDICT: APPROVE\nmore\nVERDICT: CHANGES_REQUESTED'),
                         'CHANGES_REQUESTED')
        self.assertIsNone(ats.parse_verdict('Finish with VERDICT: APPROVE or VERDICT: CHANGES_REQUESTED.'))

    # ------------------------------------------------------- read-only review
    def test_reviewer_edits_are_reverted_and_block_approval(self):
        def tamper(folder, prompt):
            (folder / 'app.py').write_text('x = 999\n')
            (folder / 'sneaky.py').write_text('print(1)\n')
            return findings_line() + '\nVERDICT: APPROVE'
        approved, *_ = self.run_loop([('claude', tamper)], conf={'max_revisions': 0})
        self.assertFalse(approved)
        self.assertEqual((self.folder / 'app.py').read_text(), 'x = 2\n')
        self.assertFalse((self.folder / 'sneaky.py').exists())
        result = [e for e in self.events if e['kind'] == 'review_result'][0]
        self.assertIn('sneaky.py', result['tampered_paths'])
        self.assertIn('reviewer modified the workspace (changes reverted)', result['blocking_reasons'])

    def test_generated_files_from_reviewer_tests_are_not_tampering(self):
        def runs_tests(folder, prompt):
            (folder / '__pycache__').mkdir()
            (folder / '__pycache__' / 'app.cpython-312.pyc').write_bytes(b'x')
            return findings_line() + '\nVERDICT: APPROVE'
        approved, *_ = self.run_loop([('claude', runs_tests)])
        self.assertTrue(approved)

    # ------------------------------------------------------- no self-review
    def test_reviewer_out_of_usage_does_not_fall_back_to_implementer(self):
        def exhausted(folder, prompt):
            raise ats.AgentUsageExhausted('claude', 'resets 11am')
        approved, reviewer, *_ = self.run_loop([('claude', exhausted)])
        self.assertFalse(approved)
        self.assertEqual([agent for agent, _ in self.calls], ['claude'])
        self.assertIn('review_skipped', self.kinds())
        self.assertIn('escalated', self.kinds())

    def test_unavailable_reviewer_is_skipped(self):
        unavailable = ats.UnavailableAgents()
        ats.mark_unavailable(unavailable, 'claude', 'usage')
        approved, *_ = self.run_loop([], unavailable=unavailable)
        self.assertFalse(approved)
        self.assertEqual(self.calls, [])

    # ------------------------------------------------------- gates
    def test_no_tests_configured_blocks_unless_allowed(self):
        review = ('claude', lambda f, p: findings_line() + '\nVERDICT: APPROVE')
        approved, *_ = self.run_loop([review], conf={'test_commands': [], 'max_revisions': 0})
        self.assertFalse(approved)
        self.events.clear()
        approved, *_ = self.run_loop([review], conf={'test_commands': [],
                                                     'allow_approval_without_tests': True})
        self.assertTrue(approved)

    def test_failing_checks_block_approval(self):
        approved, *_ = self.run_loop(
            [('claude', lambda f, p: findings_line() + '\nVERDICT: APPROVE')],
            conf={'test_commands': [FAIL], 'max_revisions': 0})
        self.assertFalse(approved)

    def test_approve_with_open_major_is_rejected(self):
        approved, *_ = self.run_loop(
            [('claude', lambda f, p: findings_line(MAJOR) + '\nVERDICT: APPROVE')],
            conf={'max_revisions': 0})
        self.assertFalse(approved)

    def test_oversized_diff_is_not_reviewed(self):
        (self.folder / 'big.txt').write_text('y' * 5000)
        approved, *_ = self.run_loop([], conf={'max_review_diff_chars': 1000})
        self.assertFalse(approved)
        self.assertEqual(self.calls, [])
        self.assertIn('review_skipped', self.kinds())

    # ------------------------------------------------------- feedback loop
    def test_fixed_finding_then_rereview_approves(self):
        def revise(folder, prompt):
            self.assertIn('F1', prompt)
            (folder / 'app.py').write_text('x = 3  # timeout added\n')
            return 'Done.\n' + responses_line({'id': 'F1', 'status': 'fixed', 'note': 'added',
                                               'reference': 'app.py:1'})

        def rereview(folder, prompt):
            self.assertIn('re-review cycle 1', prompt)
            self.assertIn('"status": "fixed"', prompt)
            return findings_line() + '\nVERDICT: APPROVE'

        approved, _, _, history = self.run_loop([
            ('claude', lambda f, p: findings_line(MAJOR) + '\nVERDICT: CHANGES_REQUESTED'),
            ('codex', revise),
            ('claude', rereview),
        ])
        self.assertTrue(approved)
        self.assertEqual(history[0]['responses'][0]['status'], 'fixed')

    def test_unanswered_major_blocks_even_if_reviewer_approves(self):
        approved, *_ = self.run_loop([
            ('claude', lambda f, p: findings_line(MAJOR) + '\nVERDICT: CHANGES_REQUESTED'),
            ('codex', lambda f, p: ((f / 'app.py').write_text('x = 4\n'), 'fixed it')[1]),
            ('claude', lambda f, p: findings_line() + '\nVERDICT: APPROVE'),
        ], conf={'max_revisions': 1})
        self.assertFalse(approved)
        final = [e for e in self.events if e['kind'] == 'review_result'][-1]
        self.assertIn('unanswered MAJOR F1 (cycle 0)', final['blocking_reasons'])
        self.assertEqual(self.kinds()[-1], 'escalated')

    def test_deferred_blocker_prevents_approval(self):
        approved, *_ = self.run_loop([
            ('claude', lambda f, p: findings_line(BLOCKER) + '\nVERDICT: CHANGES_REQUESTED'),
            ('codex', lambda f, p: responses_line({'id': 'B1', 'status': 'deferred',
                                                   'note': 'later', 'reference': '#12'})),
            ('claude', lambda f, p: findings_line() + '\nVERDICT: APPROVE'),
        ], conf={'max_revisions': 1})
        self.assertFalse(approved)
        final = [e for e in self.events if e['kind'] == 'review_result'][-1]
        self.assertIn('deferred BLOCKER B1 (cycle 0)', final['blocking_reasons'])

    def test_deferred_major_is_allowed(self):
        approved, *_ = self.run_loop([
            ('claude', lambda f, p: findings_line(MAJOR) + '\nVERDICT: CHANGES_REQUESTED'),
            ('codex', lambda f, p: responses_line({'id': 'F1', 'status': 'deferred',
                                                   'note': 'tracked', 'reference': '#13'})),
            ('claude', lambda f, p: findings_line() + '\nVERDICT: APPROVE'),
        ])
        self.assertTrue(approved)


class NormalizeTests(unittest.TestCase):
    def test_severity_aliases_and_unknown_default_to_blocking(self):
        findings = ats.normalize_findings([
            {'severity': 'critical', 'summary': 'a'}, {'severity': 'low', 'summary': 'b'},
            {'severity': 'weird', 'summary': 'c'}, {'severity': 'MAJOR', 'summary': ''}])
        self.assertEqual([f['severity'] for f in findings], ['BLOCKER', 'MINOR', 'MAJOR'])
        self.assertEqual(findings[0]['id'], 'F1')

    def test_response_status_variants(self):
        responses = ats.normalize_responses([
            {'id': 'F1', 'status': 'Not applicable'}, {'id': 'F2', 'status': 'not-applicable'},
            {'id': '<id>', 'status': 'fixed'}, {'id': 'F3', 'status': 'maybe'}])
        self.assertEqual([r['id'] for r in responses], ['F1', 'F2'])


if __name__ == '__main__':
    unittest.main()
