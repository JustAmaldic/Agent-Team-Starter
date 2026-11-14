import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ats  # noqa: E402
from ats_modules import cli_fixture, gate, screen_map, write_scope  # noqa: E402


class WriteScopeTests(unittest.TestCase):
    def test_allowed_and_external_instructions(self):
        root = '/srv/project'
        self.assertEqual(write_scope.external_targets('Fix the /api/users route and write tests', root), [])
        self.assertEqual(write_scope.external_targets('Update docs/README.md and src/../lib/a.py', root), [])
        self.assertEqual(write_scope.external_targets('Lees /etc/hosts en toon het in de UI', root), [])
        self.assertEqual(write_scope.external_targets('Sla het rapport op in ~/Desktop/rapport.md', root), ['home'])
        self.assertEqual(write_scope.external_targets('Append a line to /etc/hosts', root), ['system'])
        self.assertEqual(write_scope.external_targets('Maak een kopie in ..\\ander\\x.py', root), ['parent'])
        self.assertEqual(write_scope.external_targets('Write it to C:\\Users\\amaldi\\out.txt', root), ['home'])

    def test_reason_names_category_never_the_path(self):
        text = write_scope.reason(write_scope.external_targets('Save output in /home/amaldi/secret-folder/x'))
        self.assertIn('home folder', text)
        self.assertNotIn('amaldi', text)
        self.assertNotIn('secret-folder', text)


class WriteScopeFlowTests(unittest.TestCase):
    def flow(self, task, conf=None):
        events = []
        with mock.patch.object(ats, 'project', return_value={'project': 'p', 'test_commands': [], **(conf or {})}), \
                mock.patch.object(ats, 'execute', return_value=subprocess.CompletedProcess([], 0, str(ats.APP), '')), \
                mock.patch.object(ats, 'enabled_agents', return_value=['codex', 'claude']), \
                mock.patch.object(ats, 'route', return_value=('codex', {'codex': 2, 'claude': 0}, {'codex': ['keywords fix: +2'], 'claude': []})), \
                mock.patch.object(ats, 'phase'), mock.patch('builtins.print'), \
                mock.patch.object(ats, 'configure_test_env', side_effect=RuntimeError('reached the run')), \
                mock.patch.object(ats, 'record', side_effect=lambda kind, summary, task=None, **f: events.append(dict(kind=kind, summary=summary, **f))):
            try:
                outcome = ats._task_flow(task, 'auto', False, 'tid')
            except RuntimeError as exc:
                outcome = str(exc)
        return outcome, events

    def test_external_write_is_blocked_before_any_agent_and_logs_only_the_category(self):
        outcome, events = self.flow('Schrijf het resultaat naar ~/Desktop/geheim-plan.txt')
        self.assertEqual(outcome, 'blocked')
        scope = next(e for e in events if e['kind'] == 'write_scope')
        self.assertEqual((scope['categories'], scope['blocked']), (['home'], True))
        self.assertNotIn('geheim-plan', json.dumps([e for e in events if e['kind'] != 'assigned']))
        self.assertEqual(next(e for e in events if e['kind'] == 'agent_routing')['chosen'], 'codex')

    def test_task_inside_the_project_continues(self):
        outcome, events = self.flow('Fix the /api/users route in app/routes.py')
        self.assertEqual(outcome, 'reached the run')
        self.assertFalse([e for e in events if e['kind'] == 'write_scope'])

    def test_warn_mode_records_but_continues(self):
        outcome, events = self.flow('Save a copy in /tmp/out.json', {'write_scope': 'warn'})
        self.assertEqual(outcome, 'reached the run')
        self.assertFalse(next(e for e in events if e['kind'] == 'write_scope')['blocked'])


class FlakyTests(unittest.TestCase):
    def run_with(self, codes, conf=None):
        calls = []

        def fake(cmd, folder, timeout=0):
            calls.append(cmd)
            return SimpleNamespace(returncode=codes[len(calls) - 1], stdout=f'run {len(calls)}', stderr='')
        with mock.patch.object(ats, 'run_check', fake), mock.patch.object(ats, 'cannot_start', lambda *a: False):
            return ats.run_checks({'test_commands': ['pytest -q'], **(conf or {})}, Path('.')), calls

    def test_fails_then_passes_is_flaky_and_counts_as_passed(self):
        checks, calls = self.run_with([1, 0])
        self.assertTrue(checks[0]['passed'] and checks[0]['flaky'])
        self.assertEqual(calls, ['pytest -q', 'pytest -q --lf'])
        self.assertEqual(checks[0]['first_output'], 'run 1')

    def test_fails_twice_stays_a_failure(self):
        checks, calls = self.run_with([1, 1])
        self.assertFalse(checks[0]['passed'])
        self.assertNotIn('flaky', checks[0])
        self.assertEqual(len(calls), 2)

    def test_rerun_can_be_turned_off(self):
        checks, calls = self.run_with([1], {'flaky_rerun': False})
        self.assertEqual((checks[0]['passed'], len(calls)), (False, 1))

    def test_flaky_event_is_recorded_once(self):
        checks = [{'command': 'pytest -q', 'passed': True, 'flaky': True, 'first_output': 'boom'}]
        with mock.patch.object(ats, 'record') as record:
            ats.note_flaky(checks, 'task', cycle=1)
            ats.note_flaky(checks, 'task', cycle=1)
        self.assertEqual(record.call_count, 1)
        self.assertEqual(record.call_args.args[0], 'test_flaky')
        self.assertTrue(record.call_args.kwargs['rerun_passed'])

    def test_reviewer_gets_no_output_of_passing_checks(self):
        slim = ats.checks_for_prompt([
            {'command': 'a', 'passed': True, 'output': 'x\n' * 400 + 'OK 12 tests'},
            {'command': 'b', 'passed': False, 'output': 'Traceback detail'},
            {'command': 'c', 'passed': True, 'flaky': True, 'output': 'ok', 'first_output': 'boom', 'flaky_recorded': True}])
        self.assertEqual(slim[0]['output'], 'OK 12 tests')
        self.assertEqual(slim[1]['output'], 'Traceback detail')
        self.assertTrue(slim[2]['flaky'])
        self.assertNotIn('first_output', slim[2])


class DependencyTests(unittest.TestCase):
    DIFF = ('diff --git a/requirements.txt b/requirements.txt\n--- a/requirements.txt\n+++ b/requirements.txt\n'
            '@@ -1,1 +1,3 @@\n flask\n+requests==2.32.0\n+# comment\n'
            'diff --git a/package.json b/package.json\n--- a/package.json\n+++ b/package.json\n@@ -1,1 +1,3 @@\n'
            '+  "name": "x",\n+    "left-pad": "^1.3.0",\n+  "version": "1.0.0",\n'
            'diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,2 @@\n+import requests\n')

    def test_new_dependencies_are_found(self):
        changes = gate.dependency_changes(self.DIFF)
        self.assertEqual(changes, [{'file': 'requirements.txt', 'added': ['requests==2.32.0']},
                                   {'file': 'package.json', 'added': ['left-pad ^1.3.0']}])
        self.assertIn('requests==2.32.0', gate.dependency_note(changes))

    def test_no_dependency_files_no_note(self):
        self.assertEqual(gate.dependency_note(gate.dependency_changes(self.DIFF.split('diff --git a/app.py')[1])), '')

    def test_pr_description_lists_them(self):
        with mock.patch.dict(ats._run_flags, {'t': {'dependencies': gate.dependency_changes(self.DIFF)}}):
            self.assertIn('- requirements.txt: requests==2.32.0', ats.dependency_lines('t'))
        self.assertEqual(ats.dependency_lines('unknown'), '')


class StaleScreenMapTests(unittest.TestCase):
    def test_renamed_file_is_found_again_and_missing_one_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'ui').mkdir()
            (root / 'ui' / 'devices-page.js').write_text('x')
            (root / 'app.css').write_text('x')
            data = {'version': 1, 'screens': [{'name': 'Devices', 'files': ['static/devices-page.js', 'app.css', 'gone.js']}]}
            fresh, stale = screen_map.refresh(root, data)
            self.assertEqual(fresh['screens'][0]['files'], ['ui/devices-page.js', 'app.css'])
            self.assertEqual({(s['path'], s['now']) for s in stale},
                             {('static/devices-page.js', 'ui/devices-page.js'), ('gone.js', None)})
            self.assertEqual(data['screens'][0]['files'][0], 'static/devices-page.js')   # input untouched
            self.assertEqual([s['name'] for s in screen_map.for_files(fresh, ['ui/devices-page.js'])], ['Devices'])
            note = screen_map.stale_note(stale)
            self.assertIn('is now ui/devices-page.js', note)
            self.assertIn('gone.js (Devices) no longer exists', note)

    def test_current_map_is_not_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'a.js').write_text('x')
            data = {'version': 1, 'screens': [{'name': 'A', 'files': ['a.js']}]}
            self.assertEqual(screen_map.refresh(tmp, data), (data, []))


class RoutingTests(unittest.TestCase):
    def test_route_explains_the_choice(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ats, 'DATA', Path(tmp)), \
                mock.patch.object(ats, 'project', lambda: {'project': 'p', 'default_agent': 'codex'}), \
                mock.patch.object(ats, 'enabled_agents', lambda: ['claude', 'codex']):
            events = Path(tmp) / 'p' / 'events'
            events.mkdir(parents=True)
            for i in range(3):
                (events / f'{i}.json').write_text(json.dumps(
                    {'event_type': 'review_result', 'implementer': 'claude', 'approved': True}))
            chosen, scores, reasons = ats.route('refactor the design of the settings module')
            self.assertEqual(chosen, 'claude')
            self.assertGreater(scores['claude'], scores['codex'])
            self.assertTrue(any('keywords' in r for r in reasons['claude']))
            self.assertIn('recent reviews: +3', reasons['claude'])
            self.assertEqual(ats.select('refactor the design of the settings module'), 'claude')
            chosen, _, reasons = ats.route('iets zonder trefwoorden')   # claude still leads on history
            self.assertEqual(chosen, 'claude')


class CliFixtureTests(unittest.TestCase):
    REPORT = {'ats': 'agent-check', 'platform': 'darwin', 'agents': {
        'claude': {'version': '2.1.0 (Claude Code)', 'ok': True, 'problems': [], 'review_tail': 'secret out'},
        'codex': {'version': 'codex-cli 0.50', 'ok': False, 'review_tail': '', 'problems': [
            'review call failed: codex error: /Users/amaldi/.codex/auth.json token=sk-abcdefghijklmnop1234 not logged in '
            'for someone@example.com', 'review: ATS_FINDINGS_JSON missing or not parsable']}}}

    def test_fixture_is_redacted_and_categorised(self):
        fixture = cli_fixture.build(self.REPORT)
        text = json.dumps(fixture)
        for secret in ('amaldi', 'sk-abcdefghijklmnop1234', 'someone@example.com', 'secret out'):
            self.assertNotIn(secret, text)
        self.assertEqual(fixture['schema'], cli_fixture.SCHEMA)
        self.assertEqual(fixture['os'], 'darwin')
        self.assertEqual(fixture['agents']['codex']['error_categories'], ['marker_missing', 'not_authenticated'])
        self.assertEqual(fixture['agents']['claude']['exit'], 'ok')

    def test_compare_names_what_changed(self):
        old = cli_fixture.build(self.REPORT)
        new = json.loads(json.dumps(old))
        new['agents']['codex'].update(ok=True, error_categories=[], version='codex-cli 0.51')
        lines = cli_fixture.compare(old, new)
        self.assertEqual(len(lines), 3)
        self.assertEqual(cli_fixture.compare(old, old), [])

    def test_redact_windows_home(self):
        self.assertEqual(cli_fixture.redact(r'C:\Users\Amaldi\AppData\x'), r'<home>\AppData\x')


if __name__ == '__main__':
    unittest.main()
