"""Findings that matter are not lost: a minimum severity, a register with owners, a fix-up step
before the integration review, release files for one part, and the exact test Python for agents."""
import json
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
from ats_modules import findings_ledger, severity, test_env

PASS = [sys.executable, '-c', 'pass']
APPROVE = 'ATS_FINDINGS_JSON: []\nVERDICT: APPROVE'
REDOS = ("SECRET_TEXT has unbounded [\\w-]* on both sides of the keyword with weak anchoring; crafted "
         "4000-char input causes heavy backtracking that holds the GIL")


def git(folder, *args):
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args], cwd=folder,
                          capture_output=True, text=True, check=True).stdout


class SeverityFloorTests(unittest.TestCase):
    def test_host_blocking_and_data_loss_are_at_least_major(self):
        findings, raised = severity.raise_floor([
            {'id': 'SF1', 'severity': 'MINOR', 'summary': REDOS},
            {'id': 'F2', 'severity': 'SUGGESTION', 'summary': 'Import overwrites rows and causes data loss'},
            {'id': 'F3', 'severity': 'MINOR', 'summary': 'A crafted path allows path traversal outside the root'},
            {'id': 'F4', 'severity': 'MINOR', 'summary': 'Error message leaks the API key in the log'},
        ])
        self.assertEqual(raised, ['SF1', 'F2', 'F3', 'F4'])
        self.assertTrue(all(f['severity'] == 'MAJOR' for f in findings))
        self.assertEqual(findings[0]['raised_from'], 'MINOR')

    def test_ordinary_findings_keep_their_severity(self):
        findings, raised = severity.raise_floor([
            {'id': 'F1', 'severity': 'MINOR', 'summary': "SECRET_TEXT refuses ordinary phrases like 'Tokenvalidatie: …'"},
            {'id': 'F2', 'severity': 'MINOR', 'summary': 'Write errors escape as OSError and give a generic HTTP 500'},
            {'id': 'F3', 'severity': 'BLOCKER', 'summary': 'DoS through unbounded upload'},
        ])
        self.assertEqual(raised, [])
        self.assertEqual([f['severity'] for f in findings], ['MINOR', 'MINOR', 'BLOCKER'])

    def test_a_raised_finding_blocks_approval(self):
        findings = ats.floored([{'id': 'SF1', 'severity': 'MINOR', 'file': 'a.py', 'line': '', 'summary': REDOS}])
        reasons = ats.evaluate_approval('APPROVE', [{'command': 'x', 'passed': True}], findings, [], 'claude',
                                        'codex', {})
        self.assertIn('open BLOCKER/MAJOR findings: SF1', reasons)


PLAN = {'plan_version': 2, 'tasks': [
    {'id': 'contract', 'title': 'Template contract with tests', 'depends_on': [],
     'owned_paths': ['contract.py', 'tests/test_contract.py']},
    {'id': 'backend', 'title': 'Template storage and API with tests', 'depends_on': ['contract'],
     'owned_paths': ['templates.py', 'app.py']},
    {'id': 'screen', 'title': 'Template screen with tests', 'depends_on': ['contract'],
     'owned_paths': ['templates.js']}]}


class LedgerTests(unittest.TestCase):
    HISTORY = [
        {'subtask': 1, 'cycle': 0, 'findings': [
            {'id': 'F1', 'severity': 'MAJOR', 'file': 'contract.py', 'line': '3', 'summary': 'Missing check'}],
         'responses': [{'id': 'F1', 'status': 'fixed', 'note': 'added'}]},
        {'subtask': 1, 'cycle': 1, 'findings': [
            {'id': 'F2', 'severity': 'MINOR', 'file': 'contract.py', 'line': '23', 'summary': 'Refuses ordinary text'},
            {'id': 'SF1', 'severity': 'MINOR', 'file': 'templates.py', 'line': '9', 'summary': 'No storage message'},
            {'id': 'F3', 'severity': 'SUGGESTION', 'file': 'contract.py', 'line': '', 'summary': 'Rename'}],
         'responses': []},
    ]

    def test_keys_status_and_owner(self):
        ledger = {e['key']: e for e in findings_ledger.build(self.HISTORY, PLAN)}
        self.assertEqual(ledger['part1/review/F1']['status'], 'fixed')
        self.assertEqual(ledger['part1/review/F2']['status'], 'open')
        self.assertEqual(ledger['part1/review/F2']['owner'], 'contract')
        self.assertEqual(ledger['part1/security/SF1']['owner'], 'backend')
        attention = [e['key'] for e in findings_ledger.needs_attention(ledger.values())]
        self.assertEqual(sorted(attention), ['part1/review/F2', 'part1/security/SF1'])   # no SUGGESTION

    def test_the_owner_part_gets_its_findings(self):
        ledger = findings_ledger.build(self.HISTORY, PLAN)
        self.assertEqual([e['key'] for e in findings_ledger.for_owner(ledger, 'backend')], ['part1/security/SF1'])
        self.assertIn('part1/security/SF1', findings_ledger.owner_note(findings_ledger.for_owner(ledger, 'backend')))

    def test_only_a_review_that_got_them_can_close_them(self):
        ledger = findings_ledger.build(self.HISTORY, PLAN)
        resolved = {e['key']: e for e in findings_ledger.resolve(ledger, ['part1/review/F2'])}
        self.assertEqual(resolved['part1/review/F2']['status'], 'open')
        self.assertEqual(resolved['part1/security/SF1']['status'], 'fixed')
        self.assertIn('integration review', resolved['part1/security/SF1']['note'])
        self.assertEqual(len(findings_ledger.summary_lines(resolved.values())), 1)


class ReleaseFilesTests(unittest.TestCase):
    def test_detects_changelog_and_package_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'CHANGELOG.md').write_text('## 1.0\n')
            (root / 'pkg').mkdir()
            (root / 'pkg' / '__init__.py').write_text('VERSION = "1.2.3"\n')
            (root / 'other').mkdir()
            (root / 'other' / '__init__.py').write_text('x = 1\n')
            self.assertEqual(ats.release_files({}, root), ['CHANGELOG.md', 'pkg/__init__.py'])
            self.assertEqual(ats.release_files({'release_files': ['VERSION']}, root), ['VERSION'])
            self.assertEqual(ats.release_files({'release_files': []}, root), [])

    def test_only_the_first_part_owns_them(self):
        plan = json.loads(json.dumps(PLAN))
        plan['tasks'][1]['owned_paths'] += ['CHANGELOG.md', 'pkg/__init__.py']
        assigned = ats.assign_release_files(plan, ['CHANGELOG.md', 'pkg/__init__.py'])
        self.assertEqual(assigned['release_owner'], 'contract')
        self.assertIn('CHANGELOG.md', assigned['tasks'][0]['owned_paths'])
        self.assertNotIn('CHANGELOG.md', assigned['tasks'][1]['owned_paths'])
        self.assertEqual(ats.assign_release_files(plan, []), plan)

    def test_planner_is_told(self):
        with patch.object(ats, 'release_files', return_value=['CHANGELOG.md']):
            self.assertIn('Give them to the FIRST part', ats.release_note({}))


class AgentTestCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = pathlib.Path(self.temp.name) / '.venv'
        test_env.python_in(self.env).parent.mkdir(parents=True)
        test_env.python_in(self.env).write_text('')

    def tearDown(self):
        self.temp.cleanup()
        ats._test_env = None

    def test_agents_get_the_exact_python_and_no_full_suite(self):
        text = test_env.agent_instructions({'test_commands': ['python -m unittest discover -s tests']}, self.env)
        python = str(test_env.python_in(self.env))
        self.assertIn(python + ' -m unittest discover -s tests', text)
        self.assertIn('Do not run the whole suite yourself', text)
        self.assertIn('login shell', text)
        self.assertEqual(test_env.absolute('npm test', self.env), 'npm test')
        self.assertEqual(test_env.agent_instructions({'test_commands': []}, self.env), '')

    def test_claude_may_run_targeted_tests_with_that_python(self):
        ats._test_env = self.env
        python = str(test_env.python_in(self.env))
        allowed = ats.claude_allowed_tools({'test_commands': ['python -m unittest discover -s tests']})
        for pattern in (f'Bash({python} -m pytest:*)', f'Bash({python} -m unittest:*)',
                        'Bash(python3 -m pytest:*)', 'Bash(git diff:*)'):
            self.assertIn(pattern, allowed)


class FixupBeforeIntegrationTests(unittest.TestCase):
    """A pair whose part reviews leave a MINOR finding: the fix-up step gets it, the integration
    review is asked to verify it, and the register closes it only because that review did not list it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self.temp.name) / 'app-ats-1234'
        self.folder.mkdir()
        git(self.folder, 'init', '-q', '-b', 'main')
        (self.folder / 'app.py').write_text('base = 1\n')
        git(self.folder, 'add', '-A')
        git(self.folder, 'commit', '-q', '-m', 'init')
        git(self.folder, 'switch', '-q', '-c', 'ats/b')
        self.calls, self.events = [], []
        self.lock = threading.Lock()

    def tearDown(self):
        self.temp.cleanup()

    def test_open_finding_goes_to_fixup_and_integration(self):
        plan = {'plan_version': 2, 'tasks': [
            {'id': 'api', 'title': 'Build the API module with tests', 'depends_on': [], 'owned_paths': ['api.py']},
            {'id': 'screen', 'title': 'Build the settings screen module with tests', 'depends_on': [],
             'owned_paths': ['screen.py']}]}
        minor = ('ATS_FINDINGS_JSON: [{"id":"F1","severity":"MINOR","file":"api.py","line":"1",'
                 '"summary":"No clear message when saving fails"}]\nVERDICT: APPROVE')

        def invoke(agent, prompt, cwd, quiet=False, **kw):
            cwd = pathlib.Path(cwd)
            with self.lock:
                self.calls.append((agent, prompt))
            if 'Implement ONLY' in prompt:
                time.sleep(0.05)
                name = 'api.py' if 'Subtask 1/2' in prompt else 'screen.py'
                (cwd / name).write_text(f'by = "{agent}"\n')
                return 'built\nATS_LEARNINGS_JSON: []'
            if 'finishing a planned task before its integration review' in prompt:
                (cwd / 'api.py').write_text('by = "fixed"\n')
                return 'ATS_RESPONSES_JSON: [{"id":"part1/review/F1","status":"fixed","note":"message"}]'
            if 'INTEGRATION REVIEW' in prompt:
                return APPROVE
            return minor if 'Subtask 1/2' in prompt else APPROVE

        with patch.object(ats, 'invoke', side_effect=invoke), \
             patch.object(ats, 'record', side_effect=lambda kind, summary, task=None, **f:
                          self.events.append(dict(kind=kind, **f))), \
             patch.object(ats, 'phase'), patch.object(ats, 'capture_learning_proposals'), \
             patch.object(ats, 'codex_preflight'), patch.object(ats, 'knowledge_context', return_value=''), \
             patch.object(ats, 'plan_history', return_value=ats.plan_learning.history([])), \
             patch.object(ats, 'enabled_agents', return_value=['codex', 'claude']), \
             patch.object(ats, 'executable', return_value='x'), \
             patch.object(ats, 'integration_base', return_value=git(self.folder, 'rev-parse', 'main').strip()):
            approved, *_ = ats.subtask_flow('big task', plan, 'tid-ledger', self.folder, 'ats/b', 'codex',
                                            ats.UnavailableAgents(), {'test_commands': [PASS], 'max_revisions': 0},
                                            quiet=True)
        self.assertTrue(approved)
        fixups = [p for a, p in self.calls if 'finishing a planned task' in p]
        self.assertEqual(len(fixups), 1)
        self.assertIn('part1/review/F1', fixups[0])
        integration = [p for a, p in self.calls if 'INTEGRATION REVIEW' in p]
        self.assertIn('OPEN FINDINGS FROM THE PART REVIEWS', integration[0])
        ledger = next(e for e in self.events if e['kind'] == 'findings_ledger')
        self.assertEqual(ledger['open'], [])
        entry = next(e for e in ledger['entries'] if e['key'] == 'part1/review/F1')
        self.assertEqual(entry['status'], 'fixed')


if __name__ == '__main__':
    unittest.main()
