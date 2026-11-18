"""t68: middle rounds rerun only what failed; approval still rests on the full suite; time is measured."""
import datetime as dt
import unittest
from unittest.mock import patch

import ats
from ats_modules import run_report


def check(cmd, passed, seconds=10.0):
    return {'command': cmd, 'passed': passed, 'output': '', 'seconds': seconds}


class RetestTests(unittest.TestCase):
    def run_retest(self, conf, checks, failed, results):
        calls = []
        def fake(conf_used, folder):
            calls.append(list(conf_used['test_commands']))
            return [check(c, results[c]) for c in conf_used['test_commands']]
        with patch.object(ats, 'run_checks', side_effect=fake), patch.object(ats, 'record') as record:
            out = ats.retest_after_repair(conf, '/x', checks, failed, 't1', {}, 0)
        return out, calls, record

    def test_still_failing_does_not_rerun_passing_commands(self):
        checks = [check('unit', False), check('e2e', True, 120.0), check('lint', True, 5.0)]
        out, calls, record = self.run_retest({'test_commands': ['unit', 'e2e', 'lint']}, checks, [checks[0]],
                                             {'unit': False, 'e2e': True, 'lint': True})
        self.assertEqual(calls, [['unit']])
        self.assertEqual([c['command'] for c in out], ['unit', 'e2e', 'lint'])
        self.assertFalse(out[0]['passed'])
        self.assertTrue(out[1]['not_rerun'])
        self.assertEqual(record.call_args.kwargs['saved_seconds'], 125.0)
        self.assertFalse(all(c['passed'] for c in out))   # this round can never be approved

    def test_repaired_runs_the_full_suite_before_approval(self):
        checks = [check('unit', False), check('e2e', True)]
        out, calls, _ = self.run_retest({'test_commands': ['unit', 'e2e']}, checks, [checks[0]],
                                        {'unit': True, 'e2e': True})
        self.assertEqual(calls, [['unit'], ['unit', 'e2e']])
        self.assertTrue(all(c['passed'] and not c.get('not_rerun') for c in out))

    def test_switch_off_and_single_command_run_everything(self):
        checks = [check('unit', False), check('e2e', True)]
        _, calls, _ = self.run_retest({'test_commands': ['unit', 'e2e'], 'quick_retest': False}, checks,
                                      [checks[0]], {'unit': False, 'e2e': True})
        self.assertEqual(calls, [['unit', 'e2e']])
        one = [check('unit', False)]
        _, calls, _ = self.run_retest({'test_commands': ['unit']}, one, one, {'unit': False})
        self.assertEqual(calls, [['unit']])

    def test_checks_record_seconds(self):
        class Done:
            returncode, stdout, stderr = 0, 'ok', ''
        with patch.object(ats, 'run_check', return_value=Done()):
            result = ats.run_checks({'test_commands': ['true']}, '/x')
        self.assertIsInstance(result[0]['seconds'], float)


class ReportTests(unittest.TestCase):
    def test_report_adds_test_time_and_saving(self):
        def ev(kind, s, **f):
            stamp = (dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=s)).isoformat()
            return dict(timestamp=stamp, project='p', task_id='t', event_type=kind, summary=kind, **f)
        events = [ev('assigned', 0),
                  ev('quick_retest', 5, saved_seconds=120.0),
                  ev('review_result', 10, cycle=0, approved=False, findings=[],
                     tests=[check('unit', False, 3.0), {**check('e2e', True, 120.0), 'not_rerun': True}]),
                  ev('review_result', 20, cycle=1, approved=True, findings=[],
                     tests=[check('unit', True, 3.0), check('e2e', True, 118.0)]),
                  ev('run_finished', 30, approved=True, review_rounds=2, outcome='approved', duration_seconds=30)]
        report = run_report.build(events, 'p', 't')
        self.assertEqual(report['test_seconds'], 124.0)
        self.assertEqual(report['retest_saved_seconds'], 120.0)
        self.assertIn('Test time in review rounds: 124.0 s (quick retest saved 120.0 s)', run_report.render(report))


if __name__ == '__main__':
    unittest.main()
