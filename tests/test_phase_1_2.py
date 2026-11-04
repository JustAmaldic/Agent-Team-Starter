"""Phase 1 and 2: persistent phases, base moved, stable finding ids, plan changes on resume,
builder self-check, usage pause, live agent activity, the agent CLI contract and crash recovery."""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats
from ats_modules import checkpoints, findings_ledger, plan_learning, usage


def git(folder, *args):
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args], cwd=folder,
                          capture_output=True, text=True, check=True).stdout


class PhaseEventTests(unittest.TestCase):
    def test_phase_is_stored_as_an_event_but_sent_live_once(self):
        stored, live = [], []
        with patch.object(ats, 'record', side_effect=lambda kind, summary, task=None, live=True, **f:
                          stored.append((kind, summary, live, f))), \
             patch.object(ats, 'emit', side_effect=live.append):
            ats.phase('review', 'tid', cycle=1, worktree=pathlib.Path('/tmp/x'))
        self.assertEqual(stored[0][:3], ('phase_changed', 'review', False))
        self.assertEqual(stored[0][3]['worktree'], str(pathlib.Path('/tmp/x')))
        self.assertEqual([e['event_type'] for e in live], ['phase'])

    def test_can_be_turned_off(self):
        with patch.dict(os.environ, {'ATS_PERSIST_PHASES': '0'}), patch.object(ats, 'record') as rec, \
             patch.object(ats, 'emit'):
            ats.phase('review', 'tid')
        rec.assert_not_called()


class BaseMovedTests(unittest.TestCase):
    def test_moved_base_blocks_with_a_clear_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            git(tmp, 'init', '-q')
            git(tmp, 'commit', '-q', '--allow-empty', '-m', 'one')
            first = git(tmp, 'rev-parse', 'HEAD').strip()
            git(tmp, 'commit', '-q', '--allow-empty', '-m', 'two')
            with patch.object(ats, 'execute', side_effect=lambda cmd, cwd=None, **k: subprocess.run(
                    cmd, cwd=tmp, capture_output=True, text=True)):
                self.assertIsNone(ats.base_moved('HEAD', None))
                self.assertIsNone(ats.base_moved('HEAD', git(tmp, 'rev-parse', 'HEAD').strip()[:12]))
                reason = ats.base_moved('HEAD', first)
        self.assertIn('moved since the plan was made', reason)
        self.assertIn('plan again', reason)
        self.assertIn('Invalid', ats.base_moved('HEAD', 'not-a-sha;rm'))


class FingerprintTests(unittest.TestCase):
    def test_same_problem_keeps_its_key_when_lines_move_or_ids_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'a.py'
            path.write_text('x = 1\nvalue = risky(input)\n')
            first = findings_ledger.fingerprint({'file': 'a.py', 'line': '2', 'summary': 'Risky call'}, tmp)
            path.write_text('# comment\nx = 1\nvalue = risky(input)\n')
            moved = findings_ledger.fingerprint({'file': 'a.py', 'line': '3', 'summary': 'Other words'}, tmp)
        self.assertEqual(first, moved)
        without_line = findings_ledger.fingerprint({'file': 'a.py', 'summary': 'Unbounded regex backtracking'})
        self.assertEqual(without_line, findings_ledger.fingerprint({'file': 'a.py', 'summary': 'regex backtracking unbounded'}))

    def test_reopened_when_answered_fixed_but_found_again(self):
        history = [
            {'subtask': 1, 'cycle': 0, 'findings': [{'id': 'F1', 'severity': 'MINOR', 'file': 'a.py', 'line': '2',
                                                     'summary': 'x', 'fingerprint': 'abc'}],
             'responses': [{'id': 'F1', 'status': 'fixed'}]},
            {'subtask': 1, 'cycle': 1, 'findings': [{'id': 'F7', 'severity': 'MINOR', 'file': 'a.py', 'line': '3',
                                                     'summary': 'x again', 'fingerprint': 'abc'}],
             'responses': []},
        ]
        ledger = findings_ledger.build(history)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]['key'], 'part1/review/F1')
        self.assertEqual(ledger[0]['status'], 'reopened')
        self.assertEqual(len(findings_ledger.needs_attention(ledger)), 1)


class SelfCheckTests(unittest.TestCase):
    def test_builder_gets_the_rubric_and_the_most_common_findings(self):
        events = [{'event_type': 'review_result', 'cycle': 0, 'approved': False, 'findings': [
                      {'severity': 'MINOR', 'summary': 'No test for the empty list'},
                      {'severity': 'MINOR', 'summary': 'Textarea uses a literal colour instead of the .input class'}]},
                  {'event_type': 'review_result', 'cycle': 0, 'approved': True, 'findings': []},
                  {'event_type': 'review_result', 'cycle': 0, 'approved': False, 'review_kind': 'part', 'findings': [
                      {'severity': 'MAJOR', 'summary': 'Missing regression tests for the parser'}]}]
        hist = plan_learning.history(events)
        self.assertEqual((hist['first_reviews'], hist['first_rejected']), (3, 2))
        note = plan_learning.self_check_note(hist)
        self.assertIn('SELF-CHECK BEFORE YOU FINISH', note)
        self.assertIn('missing or weak tests (2x)', note)
        self.assertIn('2 of the last 3 first reviews', note)
        self.assertNotIn('Reviewers in this project', plan_learning.self_check_note(plan_learning.history([])))


class PlanChangeResumeTests(unittest.TestCase):
    def test_routing_change_is_reported_not_blocking(self):
        plan = {'plan_version': 2, 'tasks': [
            {'id': 'a', 'title': 'First part with tests', 'depends_on': [], 'owned_paths': ['a.py']},
            {'id': 'b', 'title': 'Second part with tests', 'depends_on': ['a'], 'owned_paths': ['b.py']}]}
        with tempfile.TemporaryDirectory() as tmp:
            work = pathlib.Path(tmp) / 'w'
            work.mkdir()
            with patch.dict(os.environ, {'ATS_MODEL_CODEX': 'one'}):
                checkpoints.save_checkpoint(tmp, 'run', plan, work, 'a' * 40, 1,
                                            [{'subtask': 1, 'implementer': 'codex', 'reviewer': 'claude'}])
            with patch.dict(os.environ, {'ATS_MODEL_CODEX': 'two'}):
                state = checkpoints.verified_resume(tmp, 'run', plan, work, 'a' * 40)
        self.assertTrue(state['routing_changed'])
        self.assertEqual(state['completed_nodes'], ['a'])


class UsagePauseTests(unittest.TestCase):
    def test_usage_limit_with_reviewed_checkpoint_pauses(self):
        events = []
        exc = ats.AgentUsageExhausted('Usage limit reached', resets='in 2 hours') \
            if 'resets' in ats.AgentUsageExhausted.__init__.__code__.co_varnames else None
        if exc is None:
            self.skipTest('AgentUsageExhausted signature differs')
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(ats, 'project', return_value={'project': 'p', 'test_commands': []}), \
             patch.object(ats, 'execute', return_value=subprocess.CompletedProcess([], 0, str(ats.APP), '')), \
             patch.object(ats, 'enabled_agents', return_value=['codex', 'claude']), \
             patch.object(ats, 'executable', return_value='x'), patch.object(ats, 'select', return_value='codex'), \
             patch.object(ats, 'fresh_base', return_value='HEAD'), patch.object(ats, 'phase'), \
             patch('builtins.print'), patch.object(ats, 'environment_problem', return_value=None), \
             patch.object(ats, 'preplan_task', side_effect=exc), \
             patch.object(ats.checkpoints, 'read_checkpoint', return_value={'completed': 2, 'pending': []}), \
             patch.object(ats, 'record', side_effect=lambda kind, summary, task=None, **f: events.append(dict(kind=kind, **f))):
            outcome = ats._task_flow('Build three parts of a feature', 'auto', False, 'tid')
        self.assertEqual(outcome, 'blocked')
        paused = next(e for e in events if e['kind'] == 'usage_paused')
        self.assertTrue(paused['resumable'])
        self.assertEqual(paused['completed'], 2)


class ActivityTests(unittest.TestCase):
    def test_codex_and_claude_steps(self):
        self.assertEqual(usage.activity_codex(json.dumps({'type': 'item.started', 'item': {
            'type': 'command_execution', 'command': 'pytest -q'}})), {'kind': 'command', 'text': 'pytest -q'})
        edit = usage.activity_codex(json.dumps({'type': 'item.completed', 'item': {
            'type': 'file_change', 'changes': [{'path': 'a.py'}]}}))
        self.assertEqual((edit['kind'], edit['paths']), ('edit', ['a.py']))
        self.assertIsNone(usage.activity_codex('plain text'))
        tool = json.dumps({'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'name': 'Edit', 'input': {'file_path': 'web/app.js'}}]}})
        self.assertEqual(usage.activity_claude(tool)['kind'], 'edit')
        self.assertEqual(usage.render_claude_line(tool), 'web/app.js\n')
        result = json.dumps({'type': 'result', 'result': 'Done', 'session_id': 's', 'usage': {}})
        self.assertEqual(usage.render_claude_line(result), 'Done\n')

    def test_stream_command_emits_live_activity_with_the_task(self):
        live = []
        line = json.dumps({'type': 'item.started', 'item': {'type': 'command_execution', 'command': 'ls'}})
        usage.set_context(task_id='tid', subtask=2, purpose='implementation')
        try:
            with patch.object(ats, 'emit', side_effect=live.append):
                ats.stream_command([sys.executable, '-c', f'print({line!r})'], '.', quiet=True,
                                   activity=usage.activity_codex, agent='codex')
        finally:
            usage.reset_context()
        self.assertEqual(live[0]['event_type'], 'agent_activity')
        self.assertEqual((live[0]['task_id'], live[0]['subtask'], live[0]['text']), ('tid', 2, 'ls'))

    def test_claude_stream_json_is_opt_in(self):
        with patch.object(ats, 'executable', return_value='claude'), patch.object(ats, 'project', return_value={}):
            plain = ats.agent_cli_command('claude', None, True, None)
            with patch.dict(os.environ, {'ATS_CLAUDE_STREAM': '1'}):
                streamed = ats.agent_cli_command('claude', None, True, None)
        self.assertIn('json', plain)
        self.assertIn('stream-json', streamed)
        self.assertIn('--verbose', streamed)


class AgentCheckTests(unittest.TestCase):
    def test_reports_what_a_cli_got_wrong(self):
        def invoke(agent, prompt, cwd, **kw):
            if 'Do not edit any file' in prompt:
                return 'Looks fine to me.'   # no markers
            (pathlib.Path(cwd) / 'calc.py').write_text('def add(a, b):\n    return a + b\n')
            return 'ATS_RESPONSES_JSON: [{"id":"F1","status":"fixed","note":"ok"}]'
        with patch.object(ats, 'executable', return_value='/bin/true'), \
             patch.object(ats, 'agent_version', return_value='9.9'), patch.object(ats, 'invoke', side_effect=invoke):
            report = ats.agent_check(['codex'])
        entry = report['agents']['codex']
        self.assertFalse(entry['ok'])
        self.assertEqual(entry['version'], '9.9')
        self.assertTrue(any('VERDICT' in p for p in entry['problems']))
        self.assertTrue(any('ATS_FINDINGS_JSON' in p for p in entry['problems']))
        self.assertFalse(any(p.startswith('fix') for p in entry['problems']))

    def test_missing_cli(self):
        with patch.object(ats, 'executable', return_value=None):
            report = ats.agent_check(['claude'])
        self.assertIn('not installed', report['agents']['claude']['problems'][0])


@unittest.skipUnless(os.environ.get('ATS_LIVE_AGENT_TESTS') == '1', 'set ATS_LIVE_AGENT_TESTS=1 to call the real CLIs')
class LiveAgentContractTests(unittest.TestCase):
    """Calls the installed Codex/Claude CLIs (costs a little usage). Run: ATS_LIVE_AGENT_TESTS=1."""

    def test_installed_clis_follow_the_ats_contract(self):
        report = ats.agent_check([a for a in ('codex', 'claude') if shutil.which(a)])
        problems = {a: r['problems'] for a, r in report['agents'].items() if not r['ok']}
        self.assertEqual(problems, {}, json.dumps(report, indent=2)[:4000])


class CrashRecoveryTests(unittest.TestCase):
    """Interrupting a run at the moments that matter leaves consistent, recoverable state."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = pathlib.Path(self.temp.name) / 'data'
        self.events = self.data / 'p' / 'events'

    def tearDown(self):
        self.temp.cleanup()

    def stored(self):
        return [json.loads(p.read_text()) for p in sorted(self.events.glob('*.json'))] if self.events.exists() else []

    def run_flow(self, fail):
        with patch.object(ats, 'DATA', self.data), \
             patch.object(ats, 'project', return_value={'project': 'p', 'test_commands': []}), \
             patch.dict(os.environ, {'ATS_EVENTS_FILE': ''}), \
             patch.object(ats, '_task_flow', side_effect=fail):
            ats.task_flow('a task for crash testing', 'codex', False)

    def test_error_and_interrupt_each_finish_the_run_once(self):
        for crash, outcome in ((RuntimeError('agent crashed'), 'error'), (KeyboardInterrupt(), 'interrupted')):
            shutil.rmtree(self.data, ignore_errors=True)
            with self.assertRaises(type(crash)):
                self.run_flow(crash)
            finished = [e for e in self.stored() if e['event_type'] == 'run_finished']
            self.assertEqual(len(finished), 1)
            self.assertEqual(finished[0]['outcome'], outcome)

    def test_events_are_whole_files_with_unique_ids(self):
        with patch.object(ats, 'DATA', self.data), patch.object(ats, 'project', return_value={'project': 'p'}), \
             patch.object(ats, 'emit'):
            for n in range(20):
                ats.record('note', f'n{n}', 'tid')
        stored = self.stored()
        self.assertEqual(len({e['event_id'] for e in stored}), 20)
        self.assertFalse(list(self.events.glob('*.tmp')))

    def test_checkpoint_write_is_atomic(self):
        work = pathlib.Path(self.temp.name) / 'w'
        work.mkdir()
        plan = ['first part with tests', 'second part with tests']
        checkpoints.save_checkpoint(self.temp.name, 'run', plan, work, 'a' * 40, 1,
                                    [{'subtask': 1, 'implementer': 'codex', 'reviewer': 'claude'}])
        with patch('os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                checkpoints.save_checkpoint(self.temp.name, 'run', plan, work, 'b' * 40, 2,
                                            [{'subtask': 1, 'implementer': 'codex', 'reviewer': 'claude'},
                                             {'subtask': 2, 'implementer': 'claude', 'reviewer': 'codex'}])
        kept = checkpoints.read_checkpoint(self.temp.name, 'run')
        self.assertEqual((kept['commit'], kept['completed']), ('a' * 40, 1))   # the old one, whole
        self.assertFalse(list((pathlib.Path(self.temp.name) / 'checkpoints').glob('.checkpoint-*')))


if __name__ == '__main__':
    unittest.main()
