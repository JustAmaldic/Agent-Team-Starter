"""Screen map, screenshots for the reviewer, and the full tests running next to the review."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats
from ats_modules import screen_map, snapshots, ui_preview

GIT = ['git', '-c', 'user.name=t', '-c', 'user.email=t@t']

INDEX_HTML = '''<!doctype html><html><head><title>Demo</title></head><body>
<nav><button data-view="devices">Apparaten</button><button data-view="settings">Instellingen</button></nav>
<section id="view-devices"><h1>Apparaten</h1></section>
<section id="view-settings" hidden><h1>Instellingen</h1></section>
<script src="app.js"></script></body></html>
'''
APP_JS = '''function renderDeviceDetail(id) {
  return id;
}
function showToggle() {}
function showDevicePage() {}
'''


def repo(folder, files):
    folder = pathlib.Path(folder)
    for name, text in files.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text, encoding='utf-8')
    subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=folder, check=True)
    subprocess.run(['git', 'add', '-A'], cwd=folder, check=True)
    subprocess.run(GIT + ['commit', '-q', '-m', 'init'], cwd=folder, check=True)
    return folder


class ScreenMapTests(unittest.TestCase):
    def test_draft_finds_view_sections_render_functions_and_file_routes(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = pathlib.Path(temp)
            (folder / 'static').mkdir()
            (folder / 'static/index.html').write_text(INDEX_HTML)
            (folder / 'static/app.js').write_text(APP_JS)
            (folder / 'pages/devices').mkdir(parents=True)
            (folder / 'pages/devices/[id].tsx').write_text('export default function DevicePage() {}\n')
            (folder / 'pages/_app.tsx').write_text('export default function App() {}\n')
            draft, added = screen_map.generate(folder)
        by_name = {s['name']: s for s in draft['screens']}
        self.assertEqual(by_name['Apparaten']['route'], '#devices')
        self.assertIn('Devices', by_name['Apparaten']['aliases'])
        self.assertEqual(by_name['Instellingen']['route'], '#settings')
        self.assertEqual(by_name['Device detail']['symbols'], ['renderDeviceDetail'])
        self.assertIn('Device page', by_name)              # show...Page counts, a plain show... toggle does not
        self.assertNotIn('Toggle', by_name)
        self.assertEqual(by_name['Id']['route'], '/devices/:id')
        self.assertTrue(all(s['generated'] for s in draft['screens']))
        self.assertEqual(added, len(draft['screens']))

    def test_generate_keeps_what_a_person_wrote(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = pathlib.Path(temp)
            (folder / 'app.js').write_text(APP_JS)
            mine = {'screens': [{'name': 'Apparaat', 'aliases': ['device detail pagina'],
                                 'symbols': ['renderDeviceDetail'], 'files': ['app.js'], 'route': '#device'}]}
            draft, added = screen_map.generate(folder, mine)
        self.assertEqual(draft['screens'][0], mine['screens'][0])
        self.assertEqual(added, 1)   # only showDevicePage is new

    def test_match_by_alias_name_compound_and_function(self):
        data = {'screens': [
            {'name': 'Device detail', 'aliases': ['apparaatpagina'], 'route': '#device', 'files': ['a.js']},
            {'name': 'Instellingen', 'aliases': ['settings'], 'route': '#settings', 'files': ['s.js'],
             'symbols': ['renderSettings']},
            {'name': 'Log', 'route': '#log', 'files': ['l.js']}]}
        names = lambda text: [s['name'] for s in screen_map.match(data, text)]
        self.assertEqual(names('Verander de knop op de Device Detail pagina'), ['Device detail'])
        self.assertEqual(names('maak de instellingenpagina rustiger'), ['Instellingen'])
        self.assertEqual(names('bug in renderSettings'), ['Instellingen'])
        self.assertEqual(names('het apparaatpagina-menu'), ['Device detail'])
        self.assertEqual(names('pas het logboek aan'), [])          # short names only match whole words
        self.assertEqual(names('fout in de log'), ['Log'])

    def test_context_names_the_files_or_lists_the_screens(self):
        data = {'screens': [{'name': 'Device detail', 'aliases': ['apparaatpagina'], 'route': '#device',
                             'files': ['static/js/device.js'], 'symbols': ['renderDevice'],
                             'tests': ['tests/test_device.py']}]}
        text = screen_map.context(data, 'Voeg een knop toe aan de apparaatpagina')
        self.assertIn('SCREENS NAMED IN THIS TASK', text)
        self.assertIn('static/js/device.js', text)
        self.assertIn('renderDevice', text)
        self.assertIn('update .ats-screens.json', text)
        listing = screen_map.context(data, 'Voeg een knop toe aan de pagina')
        self.assertIn('SCREENS OF THIS PROJECT', listing)
        self.assertNotIn('SCREENS', screen_map.context(data, 'Refactor the database layer'))
        self.assertEqual(screen_map.context(None, 'apparaatpagina'), '')

    def test_invalid_file_is_reported_not_used(self):
        with tempfile.TemporaryDirectory() as temp:
            (pathlib.Path(temp) / '.ats-screens.json').write_text('{oops')
            data, problem = screen_map.load(temp)
        self.assertIsNone(data)
        self.assertIn('not valid JSON', problem)

    def test_uncovered_only_reports_screens_in_added_lines(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = pathlib.Path(temp)
            (folder / 'app.js').write_text(APP_JS + 'function renderBilling() {}\n')
            data = {'screens': [{'name': 'Device detail', 'symbols': ['renderDeviceDetail'], 'files': ['app.js']}]}
            missing = screen_map.uncovered(folder, data, {'app.js': ['function renderBilling() {}']})
        self.assertEqual([m['name'] for m in missing], ['Billing'])

    def test_shared_prefix_carries_the_matched_screen(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = pathlib.Path(temp)
            (folder / '.ats-screens.json').write_text(json.dumps({'screens': [
                {'name': 'Device detail', 'aliases': ['apparaatpagina'], 'files': ['static/device.js']}]}))
            ats._run_flags['screen-tid'] = {'task': 'Knop op de apparaatpagina'}
            ats._screen_context.pop('screen-tid', None)
            try:
                with patch.object(ats, 'task_knowledge', return_value=''), \
                     patch.object(ats, 'repo_context', return_value=''):
                    prefix = ats.shared_prefix(folder, {}, 'screen-tid', style='')
                    off = ats.screen_context(folder, {'screen_map': False}, None)
            finally:
                ats._run_flags.pop('screen-tid', None)
                ats._screen_context.pop('screen-tid', None)
        self.assertIn('static/device.js', prefix)
        self.assertEqual(off, '')

    def test_screens_command_gives_a_reviewed_task(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = pathlib.Path(temp)
            (folder / 'app.js').write_text(APP_JS)
            with patch.object(ats, 'APP', folder):
                result = ats.screens_command(task=True)
                matched = ats.screens_command('device detail')
        self.assertFalse(result['exists'])
        self.assertIn('renderDeviceDetail', result['task'])
        self.assertIn('.ats-screens.json', result['task'])
        self.assertEqual(matched['matches'], [])
        many = {'screens': [{'name': f'Screen {i}', 'route': f'/s{i}', 'files': [f'web/s{i}.js'],
                             'symbols': [f'renderScreen{i}'], 'generated': True} for i in range(200)]}
        text = ats.screen_task(many)
        self.assertLessEqual(len(text), 4000)              # the app accepts tasks up to 4000 characters
        self.assertIn('more, look for them in the code', text)


class SnapshotTests(unittest.TestCase):
    def test_checkout_of_a_tree_is_exact_and_removed_afterwards(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = repo(temp, {'a.txt': 'one\n'})
            (folder / 'a.txt').write_text('two\n')
            (folder / 'new.txt').write_text('new\n')
            (folder / 'node_modules').mkdir()
            tree = ats.index_tree(folder)
            with snapshots.checkout(folder, tree) as copy:
                self.assertEqual((copy / 'a.txt').read_text(), 'two\n')
                self.assertTrue((copy / 'new.txt').is_file())
                self.assertTrue((copy / 'node_modules').is_dir())
                (copy / 'junk.log').write_text('written by a test')
                where = copy
            self.assertFalse(where.exists())
            listed = subprocess.run(['git', 'worktree', 'list'], cwd=folder, capture_output=True, text=True).stdout
            self.assertEqual(len(listed.strip().splitlines()), 1)
            self.assertFalse((folder / 'junk.log').exists())
            branches = subprocess.run(['git', 'branch', '--list'], cwd=folder, capture_output=True, text=True).stdout
            self.assertEqual(branches.strip(), '* main')


class PreviewPlanTests(unittest.TestCase):
    DATA = {'screens': [
        {'name': 'Device detail', 'route': '/devices/:id', 'files': ['web/device.js']},
        {'name': 'Instellingen', 'route': '#settings', 'files': ['web/settings.js', 'web/app.css']},
        {'name': 'Projecten', 'route': '/app', 'files': ['web/projects.js']}]}

    def test_settings_need_a_command_and_keep_urls_local(self):
        self.assertIsNone(ui_preview.settings({}))
        self.assertIsNone(ui_preview.settings({'ui_preview': {'enabled': False, 'command': 'x'}}))
        preview = ui_preview.settings({'ui_preview': {'command': 'python3 -m http.server {port}',
                                                      'widths': [1280, 'x', 50], 'env': {'MODE': 'demo', 'bad key': 1}}})
        self.assertEqual(preview['widths'], [1280])
        self.assertEqual(preview['env'], {'MODE': 'demo'})
        self.assertEqual(ui_preview.page_url('http://127.0.0.1:5', '#settings'), 'http://127.0.0.1:5/#settings')
        self.assertEqual(ui_preview.page_url('http://127.0.0.1:5', '/app'), 'http://127.0.0.1:5/app')
        self.assertEqual(ui_preview.page_url('http://127.0.0.1:5', 'https://example.com/x'), 'http://127.0.0.1:5/')

    def test_plan_takes_changed_screens_and_the_reference(self):
        jobs, notes = ui_preview.plan(self.DATA, ['web/settings.js'], reference='web/projects.js')
        self.assertEqual([(j['role'], j['screen']) for j in jobs],
                         [('after', 'Instellingen'), ('reference', 'Projecten')])
        jobs, notes = ui_preview.plan(self.DATA, ['web/device.js'])
        self.assertEqual(jobs, [])
        self.assertIn('has a parameter', notes[0])
        jobs, notes = ui_preview.plan({'screens': [{'name': 'Run', 'route': '/app/run/<id>', 'files': ['r.js']}]}, ['r.js'])
        self.assertEqual(jobs, [])
        jobs, notes = ui_preview.plan(None, ['web/device.js'])
        self.assertIn('.ats-screens.json', notes[0])
        self.assertFalse(ui_preview.touches_ui(['server.py', 'README.md']))
        self.assertTrue(ui_preview.touches_ui(['web/app.css']))

    @unittest.skipUnless(ui_preview.playwright_available(), 'Playwright is not installed')
    def test_capture_takes_screenshots_of_a_local_app(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = pathlib.Path(temp) / 'app'
            folder.mkdir()
            (folder / 'index.html').write_text(INDEX_HTML.replace('<script src="app.js"></script>',
                                                                  '<script>console.error("boom")</script>'))
            preview = ui_preview.settings({'ui_preview': {
                'command': f'{sys.executable} -m http.server {{port}} --bind 127.0.0.1', 'widths': [800, 390],
                'wait_ms': 0}})
            shots, problem = ui_preview.capture(folder, preview, [{'role': 'after', 'screen': 'Apparaten',
                                                                  'path': '#devices'}],
                                                pathlib.Path(temp) / 'shots', 'after-c0', dict(os.environ))
            self.assertIsNone(problem)
            self.assertEqual([s['width'] for s in shots], [800, 390])
            for shot in shots:
                self.assertNotIn('error', shot)
                self.assertTrue((pathlib.Path(temp) / 'shots' / shot['file']).is_file())
            self.assertIn('boom', shots[0]['console_errors'][0])
            note = ui_preview.reviewer_note({'shots': shots, 'notes': []}, pathlib.Path(temp) / 'shots')
        self.assertIn('after · Apparaten · 800px', note)
        self.assertIn('boom', note)

    def test_an_app_that_does_not_start_is_a_note_not_a_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            preview = ui_preview.settings({'ui_preview': {'command': f'{sys.executable} -c "raise SystemExit(3)"',
                                                          'timeout': 5}})
            shots, problem = ui_preview.capture(temp, preview, [{'role': 'after', 'screen': 'X', 'path': '/'}],
                                                pathlib.Path(temp) / 'shots', 'after-c0', dict(os.environ))
        self.assertEqual(shots, [])
        self.assertIn('exit code 3', problem)

    def test_claude_gets_read_access_to_the_screenshots(self):
        ats.usage_meter.set_context(read_dirs=['/tmp/shots-x'])
        try:
            command = ats.claude_print_command()
        finally:
            ats.usage_meter.set_context(read_dirs=[])
        self.assertIn('/tmp/shots-x', command)
        self.assertEqual(command[command.index('/tmp/shots-x') - 1], '--add-dir')
        self.assertNotIn('/tmp/shots-x', ats.claude_print_command())


class ParallelReviewTests(unittest.TestCase):
    def run_loop(self, test_code, reviews, conf_extra=None):
        """review_loop on a tiny repo; returns (approved, prompts, events, folder state)."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        folder = repo(temp.name, {'app.py': 'x = 1\n', 'check.py': test_code})
        (folder / 'app.py').write_text('x = 2\n')
        prompts, events = [], []
        answers = list(reviews)

        def fake(agent, prompt, cwd, quiet=False, **kwargs):
            prompts.append(prompt)
            if 'Review ONLY' in prompt:
                return answers.pop(0)
            (pathlib.Path(cwd) / 'app.py').write_text('x = 3\n')
            return 'fixed\nATS_RESPONSES_JSON: []'

        conf = {'test_commands': [f'{sys.executable} check.py'], 'max_revisions': 1,
                'pre_review_gate': True, **(conf_extra or {})}
        gate = [{'name': 'targeted tests', 'level': 'targeted', 'status': 'passed', 'detail': ''}]
        with patch.object(ats, 'invoke', side_effect=fake), \
             patch.object(ats, 'record', side_effect=lambda kind, summary, task=None, **f: events.append((kind, f))), \
             patch.object(ats, 'phase', side_effect=lambda stage, task_id, **f: events.append(('phase:' + stage, f))), \
             patch.object(ats, 'pre_review_gate', return_value=(gate, 'codex', None)), \
             patch.object(ats, 'shared_prefix', return_value=''), \
             patch.object(ats, 'enabled_agents', return_value=['codex', 'claude']), \
             patch.object(ats, 'snapshot_parent', return_value=pathlib.Path(outside.name)), \
             patch('sys.stderr'):
            approved, *_ = ats.review_loop('task', 'tid', folder, 'ats/b', 'codex', ats.UnavailableAgents(),
                                           conf, quiet=True)
        return approved, prompts, events, folder

    APPROVE = 'ok\nATS_FINDINGS_JSON: []\nVERDICT: APPROVE'

    def test_review_starts_while_the_tests_run_and_approval_waits_for_them(self):
        # The test sleeps and writes a file; the review must not wait for it, and the file must not
        # land in the worktree (it would count as reviewer tampering).
        code = ('import time, pathlib\ntime.sleep(1.5)\npathlib.Path("written-by-test.log").write_text("x")\n')
        started = time.monotonic()
        approved, prompts, events, folder = self.run_loop(code, [self.APPROVE])
        self.assertTrue(approved)
        review = next(p for p in prompts if 'Review ONLY' in p)
        self.assertIn('running in parallel with this review', review)
        self.assertIn('TESTS RUN IN PARALLEL', review)
        kinds = [k for k, _ in events]
        self.assertIn('tests_parallel', kinds)
        self.assertFalse((folder / 'written-by-test.log').exists())
        result = next(f for k, f in events if k == 'review_result')
        self.assertEqual(result['tampered_paths'], [])
        self.assertTrue(result['tests'][0]['passed'])
        self.assertGreater(time.monotonic() - started, 1.4)   # it did wait for the tests

    def test_failing_suite_blocks_approval_and_goes_back_to_the_builder(self):
        code = 'import pathlib, sys\nsys.exit(0 if "3" in pathlib.Path("app.py").read_text() else 1)\n'
        approved, prompts, events, folder = self.run_loop(code, [self.APPROVE, self.APPROVE])
        self.assertTrue(approved)
        revision = next(p for p in prompts if 'Review ONLY' not in p)
        self.assertIn('ran in parallel with the review, FAILED', revision)
        results = [f for k, f in events if k == 'review_result']
        self.assertFalse(results[0]['approved'])
        self.assertIn('failing checks', ' '.join(results[0]['blocking_reasons']))
        self.assertTrue(results[1]['approved'])

    @unittest.skipUnless(ui_preview.playwright_available(), 'Playwright is not installed')
    def test_reviewer_gets_before_and_after_screenshots(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder = repo(temp.name, {'index.html': INDEX_HTML, 'check.py': 'pass\n', '.ats-screens.json': json.dumps(
            {'screens': [{'name': 'Apparaten', 'route': '#devices', 'files': ['index.html']}]})})
        (folder / 'index.html').write_text(INDEX_HTML.replace('<h1>Apparaten</h1>', '<h1>Mijn apparaten</h1>'))
        prompts, events = [], []
        conf = {'test_commands': [f'{sys.executable} check.py'], 'max_revisions': 0, 'ui_preview': {
            'command': f'{sys.executable} -m http.server {{port}} --bind 127.0.0.1', 'widths': [800], 'wait_ms': 0}}
        gate = [{'name': 'targeted tests', 'level': 'targeted', 'status': 'passed', 'detail': ''}]
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        state = pathlib.Path(outside.name) / 'state' / 'data'
        with patch.object(ats, 'invoke', side_effect=lambda a, prompt, cwd, **k: prompts.append(prompt) or self.APPROVE), \
             patch.object(ats, 'record', side_effect=lambda kind, summary, task=None, **f: events.append((kind, f))), \
             patch.object(ats, 'phase'), \
             patch.object(ats, 'pre_review_gate', return_value=(gate, 'codex', None)), \
             patch.object(ats, 'shared_prefix', return_value=''), \
             patch.object(ats, 'enabled_agents', return_value=['codex', 'claude']), \
             patch.object(ats, 'state_directory', return_value=state), \
             patch('sys.stderr'):
            approved, *_ = ats.review_loop('task', 'tid', folder, 'ats/b', 'codex', ats.UnavailableAgents(),
                                           conf, quiet=True)
            preview = next(f for k, f in events if k == 'ui_preview')
            files = sorted(p.name for p in pathlib.Path(preview['directory']).glob('*.png'))
        self.assertTrue(approved)
        self.assertEqual(files, ['after-c0-apparaten-800.png', 'before-apparaten-800.png'])
        review = prompts[0]
        self.assertIn('SCREENSHOTS of the changed UI', review)
        self.assertIn('before · Apparaten · 800px', review)
        self.assertIn('after · Apparaten · 800px', review)
        self.assertEqual(next(f for k, f in events if k == 'review_result')['tampered_paths'], [])

    def test_a_review_that_stops_early_waits_for_the_running_tests(self):
        # Otherwise the tests' checkout is still in use when the worktree is removed (fails on Windows).
        finished = []
        real = ats.snapshot_checks

        def slow(conf, folder, tree):
            result = real(conf, folder, tree)
            finished.append(True)
            return result

        def exhausted(*args, **kwargs):
            raise ats.AgentUsageExhausted('claude', 'resets 11am')

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        folder = repo(temp.name, {'app.py': 'x = 1\n', 'check.py': 'import time\ntime.sleep(1)\n'})
        (folder / 'app.py').write_text('x = 2\n')
        gate = [{'name': 'targeted tests', 'level': 'targeted', 'status': 'passed', 'detail': ''}]
        with patch.object(ats, 'invoke', side_effect=exhausted), \
             patch.object(ats, 'snapshot_checks', side_effect=slow), \
             patch.object(ats, 'snapshot_parent', return_value=pathlib.Path(outside.name)), \
             patch.object(ats, 'record'), patch.object(ats, 'phase'), \
             patch.object(ats, 'pre_review_gate', return_value=(gate, 'codex', None)), \
             patch.object(ats, 'shared_prefix', return_value=''), \
             patch.object(ats, 'enabled_agents', return_value=['codex', 'claude']), \
             patch('sys.stderr'):
            approved, *_ = ats.review_loop('task', 'tid', folder, 'ats/b', 'codex', ats.UnavailableAgents(),
                                           {'test_commands': [f'{sys.executable} check.py'], 'max_revisions': 0},
                                           quiet=True)
        self.assertFalse(approved)
        self.assertEqual(finished, [True])

    def test_off_switch_and_missing_program_keep_the_old_order(self):
        approved, prompts, events, _ = self.run_loop('pass\n', [self.APPROVE], {'parallel_review_tests': False})
        self.assertTrue(approved)
        self.assertNotIn('TESTS RUN IN PARALLEL', next(p for p in prompts if 'Review ONLY' in p))
        self.assertNotIn('tests_parallel', [k for k, _ in events])
        self.assertFalse(ats.commands_can_start({'test_commands': ['definitely-not-here-xyz -m x']}, '.'))
        self.assertTrue(ats.commands_can_start({'test_commands': [[sys.executable, '-c', 'pass']]}, '.'))


if __name__ == '__main__':
    unittest.main()
