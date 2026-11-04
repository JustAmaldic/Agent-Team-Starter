"""Token efficiency without weaker review: usage metering, sessions, gate, review scope, budgets,
context packages, knowledge selection, models per phase and the stable prompt prefix."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats
from ats_modules import budget as budgets
from ats_modules import context_pack, dag, gate, knowledge_select, review_scope, usage

PASS = [sys.executable, '-c', 'pass']
APPROVE = 'ATS_FINDINGS_JSON: []\nVERDICT: APPROVE'
MAJOR = {'id': 'F1', 'severity': 'MAJOR', 'file': 'app.py', 'line': '1', 'summary': 'No timeout on the request'}

CLAUDE_RESULT = json.dumps({
    'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'done\nVERDICT: APPROVE',
    'session_id': 'sess-claude-1', 'num_turns': 3,
    'usage': {'input_tokens': 120, 'cache_creation_input_tokens': 4000, 'cache_read_input_tokens': 9000,
              'output_tokens': 300},
    'modelUsage': {'claude-sonnet-x': {}}})
CODEX_LINES = '\n'.join(json.dumps(e) for e in [
    {'type': 'thread.started', 'thread_id': 'thread-codex-1'},
    {'type': 'turn.started'},
    {'type': 'item.started', 'item': {'id': 'i1', 'type': 'command_execution', 'command': 'pytest -q'}},
    {'type': 'item.completed', 'item': {'id': 'i2', 'type': 'agent_message', 'text': 'Implemented.'}},
    {'type': 'turn.completed', 'usage': {'input_tokens': 25000, 'cached_input_tokens': 20000, 'output_tokens': 700}},
    {'type': 'item.completed', 'item': {'id': 'i3', 'type': 'agent_message',
                                        'text': 'ATS_RESPONSES_JSON: [{"id":"F1","status":"fixed","note":"t"}]'}},
    {'type': 'turn.completed', 'usage': {'input_tokens': 1000, 'cached_input_tokens': 0, 'output_tokens': 50}},
]) + '\n'


def git(folder, *args):
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args], cwd=folder,
                          capture_output=True, text=True, check=True).stdout


def make_repo(root, files):
    git(root, 'init', '-q', '-b', 'main')
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(root, 'add', '-A')
    git(root, 'commit', '-q', '-m', 'init')


class UsageParsingTests(unittest.TestCase):
    def test_claude_result_object(self):
        parsed = usage.parse_claude(CLAUDE_RESULT + '\n')
        self.assertEqual(parsed['text'], 'done\nVERDICT: APPROVE')
        self.assertEqual(parsed['session_id'], 'sess-claude-1')
        self.assertEqual(parsed['usage'], {'input_tokens': 120, 'output_tokens': 300,
                                           'cache_read_tokens': 9000, 'cache_write_tokens': 4000})
        self.assertEqual(parsed['model'], 'claude-sonnet-x')
        self.assertIsNone(usage.parse_claude('plain text answer'))

    def test_codex_jsonl_keeps_new_and_cached_input_apart(self):
        parsed = usage.parse_codex(CODEX_LINES)
        self.assertEqual(parsed['session_id'], 'thread-codex-1')
        self.assertEqual(parsed['usage'], {'input_tokens': 6000, 'output_tokens': 750,
                                           'cache_read_tokens': 20000, 'cache_write_tokens': None})
        self.assertIn('Implemented.', parsed['text'])
        # markers survive exactly as in text mode
        self.assertEqual(ats.normalize_responses(ats.parse_marked_json(parsed['text'], 'ATS_RESPONSES_JSON'))[0]['id'], 'F1')
        self.assertIsNone(usage.parse_codex('tokens used\n1,234\n'))

    def test_legacy_codex_messages(self):
        lines = '\n'.join(json.dumps(e) for e in [
            {'id': '0', 'msg': {'type': 'session_configured', 'session_id': 'old-1', 'model': 'gpt-x'}},
            {'id': '1', 'msg': {'type': 'agent_message', 'message': 'VERDICT: APPROVE'}},
            {'id': '2', 'msg': {'type': 'token_count', 'info': {'total_token_usage': {
                'input_tokens': 500, 'cached_input_tokens': 100, 'output_tokens': 40}}}}])
        parsed = usage.parse_codex(lines)
        self.assertEqual((parsed['session_id'], parsed['model'], parsed['text']), ('old-1', 'gpt-x', 'VERDICT: APPROVE'))
        self.assertEqual(parsed['usage']['input_tokens'], 400)

    def test_live_rendering_hides_json(self):
        lines = CODEX_LINES.splitlines()
        self.assertEqual(usage.render_codex_line(lines[2] + '\n'), '$ pytest -q\n')
        self.assertEqual(usage.render_codex_line(lines[3] + '\n'), 'Implemented.\n')
        self.assertIsNone(usage.render_codex_line(lines[1] + '\n'))
        self.assertEqual(usage.render_codex_line('plain\n'), 'plain\n')
        self.assertEqual(usage.render_claude_line(CLAUDE_RESULT + '\n'), 'done\nVERDICT: APPROVE\n')

    def test_billable_counts_cache_reads_at_a_tenth(self):
        self.assertEqual(usage.billable({'input_tokens': 100, 'output_tokens': 10, 'cache_read_tokens': 1000,
                                         'cache_write_tokens': 5}), 215)


class StructuredInvokeTests(unittest.TestCase):
    def run_invoke(self, agent, stdout, **kwargs):
        seen = []

        def fake_stream(command, cwd, timeout, quiet, stdin_text=None, render=None, **kw):
            seen.append((command, render))
            return subprocess.CompletedProcess(command, 0, stdout, '')

        events = []
        with patch.object(ats, 'executable', return_value=agent), \
             patch.object(ats, 'agent_command', side_effect=lambda *a: list(a)), \
             patch.object(ats, 'stream_command', side_effect=fake_stream), \
             patch.object(ats, 'record', side_effect=lambda kind, summary, task=None, **f: events.append(dict(kind=kind, **f))), \
             patch.dict(os.environ, {'ATS_STRUCTURED_OUTPUT': '1'}):
            usage.reset_context()
            usage.set_context(task_id='tid', subtask=2, cycle=1, purpose='revision')
            out = ats.invoke(agent, 'prompt', '.', quiet=True, **kwargs)
            usage.reset_context()
        return out, seen, events

    def test_claude_json_command_resume_and_usage_event(self):
        out, seen, events = self.run_invoke('claude', CLAUDE_RESULT, resume='sess-claude-1')
        command = seen[0][0]
        self.assertEqual(command[:2], ['claude', '-p'])
        self.assertEqual(command[-4:], ['--output-format', 'json', '--resume', 'sess-claude-1'])
        self.assertTrue(out.startswith('done\nVERDICT: APPROVE'))
        event = events[0]
        self.assertEqual((event['kind'], event['agent'], event['purpose'], event['subtask'], event['cycle']),
                         ('agent_usage', 'claude', 'revision', 2, 1))
        self.assertEqual(event['cache_write_tokens'], 4000)
        self.assertTrue(event['session_resumed'])
        self.assertNotIn('prompt', json.dumps(event))

    def test_codex_json_and_resume_keep_stdin_marker_last(self):
        with patch.dict(os.environ, {'ATS_MODEL_CODEX': 'gpt-main'}):
            _, seen, _ = self.run_invoke('codex', CODEX_LINES, resume='thread-codex-1')
        self.assertEqual(seen[0][0], ['codex', 'exec', '--approve-for-me', '--model', 'gpt-main', '--json',
                                      'resume', 'thread-codex-1', '-'])
        self.assertIs(seen[0][1], usage.render_codex_line)

    def test_budget_in_context_counts_the_call(self):
        budget = budgets.Budget(tokens=100)
        seen = []

        def fake_stream(command, cwd, timeout, quiet, stdin_text=None, render=None, **kw):
            seen.append(command)
            return subprocess.CompletedProcess(command, 0, CLAUDE_RESULT, '')

        with patch.object(ats, 'executable', return_value='claude'), \
             patch.object(ats, 'stream_command', side_effect=fake_stream), patch.object(ats, 'record'), \
             patch.dict(os.environ, {'ATS_STRUCTURED_OUTPUT': '1'}):
            usage.reset_context()
            usage.set_context(task_id='t', budget=budget)
            ats.invoke('claude', 'p', '.', quiet=True)
            usage.reset_context()
        self.assertEqual(budget.used_tokens, 120 + 300 + 4000 + 900)
        self.assertIn('token budget spent', budget.exceeded())

    def test_cli_without_json_falls_back_to_text(self):
        outputs = [subprocess.CompletedProcess([], 2, '', "error: unexpected argument '--json' found"),
                   subprocess.CompletedProcess([], 0, 'plain answer', '')]
        commands = []

        def fake_stream(command, cwd, timeout, quiet, stdin_text=None, **kw):
            commands.append(command)
            return outputs.pop(0)

        with patch.object(ats, 'executable', return_value='codex'), \
             patch.object(ats, 'agent_command', side_effect=lambda *a: list(a)), \
             patch.object(ats, 'stream_command', side_effect=fake_stream), \
             patch.object(ats, '_structured_off', set()), \
             patch('builtins.print'), patch.dict(os.environ, {'ATS_STRUCTURED_OUTPUT': '1'}):
            self.assertEqual(ats.invoke('codex', 'p', '.', quiet=True).strip(), 'plain answer')
            self.assertFalse(ats.structured_output('codex'))
            with self.assertRaises(ats.SessionResumeFailed):
                ats.invoke('codex', 'p', '.', quiet=True, resume='x')
        self.assertIn('--json', commands[0])
        self.assertNotIn('--json', commands[1])


class SessionReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self.temp.name)
        make_repo(self.folder, {'app.py': 'x = 1\n'})
        (self.folder / 'app.py').write_text('x = 2\n')

    def tearDown(self):
        self.temp.cleanup()
        usage.reset_context()

    def call(self, invoke, **kwargs):
        with patch.object(ats, 'invoke', side_effect=invoke), patch.object(ats, 'record') as record, \
             patch.object(ats, 'enabled_agents', return_value=['codex', 'claude']), \
             patch.object(ats, 'executable', return_value='x'), patch.object(ats, 'codex_preflight'):
            result = ats.invoke_with_fallback('codex', 'FULL', self.folder, ats.UnavailableAgents(), 'tid', **kwargs)
        return result, record

    def test_revision_continues_the_session_only_while_the_worktree_is_unchanged(self):
        calls = []

        def invoke(agent, prompt, cwd, quiet=False, resume=None):
            calls.append((prompt, resume))
            usage.remember_call({'agent': agent, 'session_id': 'S1', 'usage': {'input_tokens': 10}})
            return 'ok'

        with patch.object(ats, 'SESSIONS', usage.Sessions()):
            self.call(invoke, purpose='implementation')
            self.call(invoke, purpose='revision', resume_prompt='SHORT')
            self.assertEqual(calls[-1], ('SHORT', 'S1'))
            (self.folder / 'app.py').write_text('x = 3\n')   # someone else changed the worktree
            _, record = self.call(invoke, purpose='revision', resume_prompt='SHORT')
            self.assertEqual(calls[-1], ('FULL', None))
            self.assertEqual(record.call_args_list[0].args[0], 'session_handoff')

    def test_reviews_never_resume(self):
        calls = []

        def invoke(agent, prompt, cwd, quiet=False, resume=None):
            calls.append(resume)
            usage.remember_call({'agent': agent, 'session_id': 'S1', 'usage': {}})
            return 'ok'

        with patch.object(ats, 'SESSIONS', usage.Sessions()):
            self.call(invoke, purpose='implementation')
            self.call(invoke, purpose='review', resume_prompt='SHORT', fallback=False)
        self.assertEqual(calls, [None, None])

    def test_failed_resume_falls_back_to_a_fresh_session(self):
        calls = []

        def invoke(agent, prompt, cwd, quiet=False, resume=None):
            calls.append((prompt, resume))
            if resume:
                raise ats.SessionResumeFailed('expired')
            usage.remember_call({'agent': agent, 'session_id': 'S1', 'usage': {}})
            return 'ok'

        with patch.object(ats, 'SESSIONS', usage.Sessions()):
            self.call(invoke, purpose='implementation')
            self.call(invoke, purpose='revision', resume_prompt='SHORT')
        self.assertEqual(calls[1:], [('SHORT', 'S1'), ('FULL', None)])

    def test_session_limits(self):
        sessions = usage.Sessions()
        sessions.remember('k', 'S', 'tree', {'input_tokens': 10})
        self.assertEqual(sessions.usable('k', 'tree'), ('S', None))
        self.assertIsNone(sessions.usable('k', 'other')[0])
        sessions.remember('k', 'S', 'tree', {'cache_read_tokens': 3000000})   # cache reads do not count
        self.assertEqual(sessions.usable('k', 'tree'), ('S', None))
        sessions.remember('k', 'S', 'tree', {'input_tokens': 300000, 'output_tokens': 150000})
        self.assertIn('context', sessions.usable('k', 'tree')[1])
        for _ in range(4):
            sessions.remember('j', 'T', 'tree', {})
        self.assertIn('turns', sessions.usable('j', 'tree')[1])

    def test_worktree_tree_leaves_the_index_alone(self):
        before = git(self.folder, 'status', '--porcelain')
        tree = ats.worktree_tree(self.folder)
        self.assertTrue(tree)
        self.assertEqual(git(self.folder, 'status', '--porcelain'), before)
        (self.folder / 'app.py').write_text('x = 4\n')
        self.assertNotEqual(ats.worktree_tree(self.folder), tree)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self.temp.name)
        make_repo(self.folder, {'calc.py': 'def add(a, b):\n    return a + b\n',
                                'tests/test_calc.py': 'from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n',
                                'tests/test_other.py': 'def test_x():\n    assert True\n'})

    def tearDown(self):
        self.temp.cleanup()

    def diff(self):
        git(self.folder, 'add', '-A')
        return git(self.folder, 'diff', '--cached', 'HEAD')

    def gate(self, conf=None):
        return gate.run_gate(conf or {}, self.folder, self.diff(), ats.tracked_files(self.folder), ats.run_check)

    def status(self, results, name):
        return next(r['status'] for r in results if r['name'] == name)

    def test_clean_change_passes_and_untestable_checks_are_skipped(self):
        (self.folder / 'calc.py').write_text('def add(a, b):\n    return b + a\n')
        results = self.gate()
        self.assertEqual(self.status(results, 'python syntax'), gate.PASSED)
        self.assertEqual(self.status(results, 'json syntax'), gate.SKIPPED)
        self.assertEqual(self.status(results, 'targeted tests'), gate.SKIPPED)   # no pytest command known
        self.assertFalse(gate.failed(results))

    def test_syntax_conflict_markers_and_secrets_fail_without_showing_the_secret(self):
        token = 'ghp_' + 'a1B2c3D4' * 5
        (self.folder / 'calc.py').write_text(f'<<<<<<< HEAD\ndef add(a, b:\nTOKEN = "{token}"\n>>>>>>> part\n')
        results = self.gate()
        failed = {r['name'] for r in gate.failed(results)}
        self.assertEqual(failed, {'conflict markers', 'secrets in diff', 'python syntax'})
        self.assertNotIn(token, json.dumps(results))
        self.assertEqual(self.status(results, 'targeted tests'), gate.SKIPPED)

    def test_related_tests_are_found_by_name_and_import(self):
        (self.folder / 'calc.py').write_text('def add(a, b):\n    return a + b + 0\n')
        tests = gate.related_tests(self.folder, ['calc.py'], ats.tracked_files(self.folder))
        self.assertEqual(tests, ['tests/test_calc.py'])
        command = gate.targeted_command({'test_commands': ['python3 -m pytest -q tests']}, tests)
        self.assertEqual(command, ['python3', '-m', 'pytest', '-q', 'tests/test_calc.py'])
        custom = gate.targeted_command({'targeted_test_command': 'run-tests {files} --fast'}, tests)
        self.assertEqual(custom, ['run-tests', 'tests/test_calc.py', '--fast'])

    def test_missing_tool_is_unavailable_never_passed(self):
        (self.folder / 'ruff.toml').write_text('line-length = 100\n')
        (self.folder / 'calc.py').write_text('def add(a, b):\n    return a + b + 1\n')
        with patch.object(gate.shutil, 'which', return_value=None):
            results = self.gate()
        self.assertEqual(self.status(results, 'ruff'), gate.UNAVAILABLE)
        missing = gate.result('x', gate.FAST, gate.UNAVAILABLE)
        self.assertFalse(gate.failed([missing]))
        command = self.gate({'gate_commands': [{'name': 'lint', 'command': ['definitely-not-a-tool-xyz']}]})
        self.assertEqual(self.status(command, 'lint'), gate.UNAVAILABLE)

    def test_targeted_tests_run_with_configured_command(self):
        (self.folder / 'calc.py').write_text('def add(a, b):\n    return a - b\n')
        failing = [sys.executable, '-c', 'import sys; print("1 failed"); sys.exit(1)']
        results = self.gate({'targeted_test_command': failing})
        self.assertEqual(self.status(results, 'targeted tests'), gate.FAILED)
        results = self.gate({'targeted_test_command': PASS})
        self.assertEqual(self.status(results, 'targeted tests'), gate.PASSED)


class ReviewScopeTests(unittest.TestCase):
    LOW, HIGH = {'level': 'low', 'reasons': []}, {'level': 'high', 'reasons': ['authentication']}

    def stats(self, files, lines, renamed=0):
        return {'files': files, 'lines': lines, 'added': lines, 'removed': 0, 'renamed': renamed}

    def test_levels(self):
        self.assertEqual(review_scope.review_level('t', self.stats(['README.md', 'tests/test_a.py'], 20), self.LOW, {})[0], 'light')
        self.assertEqual(review_scope.review_level('t', self.stats(['app.py'], 20), self.LOW, {})[0], 'standard')
        self.assertEqual(review_scope.review_level('t', self.stats(['README.md'], 500), self.LOW, {})[0], 'standard')
        self.assertEqual(review_scope.review_level('t', self.stats(['README.md'], 5), self.HIGH, {})[0], 'deep')
        self.assertEqual(review_scope.review_level('t', self.stats(['README.md'], 5), self.LOW,
                                                   {'review_level_min': 'standard'})[0], 'standard')
        self.assertEqual(review_scope.review_level('t', self.stats(['package.json'], 2), self.LOW, {})[0], 'standard')

    def test_delta_or_full(self):
        full = self.stats(['a.py', 'b.py'], 300)
        self.assertEqual(review_scope.rereview_mode(0, self.stats(['a.py'], 5), full, self.LOW, {})[0], 'full')
        self.assertEqual(review_scope.rereview_mode(1, self.stats(['a.py'], 20), full, self.LOW, {})[0], 'delta')
        self.assertEqual(review_scope.rereview_mode(1, self.stats(['a.py'], 250), full, self.LOW, {})[0], 'full')
        self.assertEqual(review_scope.rereview_mode(1, self.stats(['requirements.txt'], 1), full, self.LOW, {})[0], 'full')
        self.assertEqual(review_scope.rereview_mode(1, self.stats(['a.py'], 5), full, self.HIGH, {})[0], 'full')
        self.assertEqual(review_scope.rereview_mode(1, self.stats(['a.py'], 5, renamed=6), full, self.LOW, {})[0], 'full')
        self.assertEqual(review_scope.rereview_mode(1, self.stats(['a.py'], 5), full, self.LOW, {'delta_review': False})[0], 'full')

    def test_diff_stats(self):
        diff = ('diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1,2 @@\n-x = 1\n+x = 2\n+y = 3\n'
                'diff --git a/old.py b/old.py\ndeleted file mode 100644\n--- a/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-z\n')
        stats = review_scope.diff_stats(diff)
        self.assertEqual((stats['files'], stats['added'], stats['removed']), (['a.py'], 2, 2))


class BudgetTests(unittest.TestCase):
    def test_tokens_time_and_repairs(self):
        now = [0.0]
        budget = budgets.Budget(tokens=1000, seconds=60, repairs=2, clock=lambda: now[0])
        budget.add({'input_tokens': 500})
        self.assertIsNone(budget.exceeded())
        budget.add({'output_tokens': 600})
        self.assertIn('token budget', budget.exceeded())
        self.assertTrue(budget.take_repair() and budget.take_repair())
        self.assertFalse(budget.take_repair())
        now[0] = 61
        self.assertEqual(budget.snapshot()['seconds_used'], 61)

    def test_multiplier_and_part_size(self):
        with patch.dict(os.environ, {'ATS_BUDGET_MULTIPLIER': '2'}):
            self.assertEqual(budgets.Budget(tokens=100).tokens, 200)
        small = budgets.Budget.for_part({'subtask_token_budget': 600}, ['a'])
        large = budgets.Budget.for_part({'subtask_token_budget': 600}, ['a', 'b', 'c', 'd', 'e'])
        self.assertGreater(large.tokens, small.tokens)

    def test_stuck_detector(self):
        stuck = budgets.StuckDetector()
        self.assertIsNone(stuck.same_failure((('ruff', 'E501'),), 'gate'))
        self.assertIsNone(stuck.same_failure((('pytest', 'x'),), 'tests'))   # channels are separate
        self.assertIn('same check', stuck.same_failure((('ruff', 'E501'),), 'gate'))
        finding = {'id': 'F1', 'file': 'a.py', 'summary': 'No timeout on the HTTP request'}
        stuck.answered([finding], [{'id': 'F1', 'status': 'fixed'}])
        self.assertIsNone(stuck.reopened([dict(finding, id='F7')]))
        stuck.answered([finding], [{'id': 'F1', 'status': 'fixed'}])
        self.assertIn('came back', stuck.reopened([dict(finding, id='F9')]))
        self.assertIsNotNone(stuck.no_progress('t', 't'))
        self.assertIsNone(stuck.no_progress('t', 'u'))


class ContextPackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.folder = self.root / 'repo'
        self.folder.mkdir()
        make_repo(self.folder, {
            'README.md': '# App\n', 'pyproject.toml': '[project]\nname="app"\n',
            'app/main.py': 'from app import store\n\ndef main():\n    return store.load()\n',
            'app/store.py': 'class Store:\n    def load(self):\n        return 1\n\ndef load():\n    return Store().load()\n',
            'tests/test_store.py': 'from app.store import load\n'})
        self.cache = self.root / 'cache'

    def tearDown(self):
        self.temp.cleanup()

    def test_layers_are_compact_cached_and_keyed_by_content(self):
        tree = ats.worktree_tree(self.folder)
        repo = context_pack.repository_layer(self.folder, tree, self.cache)
        self.assertIn('Key files: README.md, pyproject.toml', repo)
        self.assertIn('Entry points: app/main.py', repo)
        part = context_pack.part_layer(self.folder, tree, self.cache, 'Store loading', ['app/store.py'])
        self.assertIn('class Store:', part)
        self.assertIn('Related tests: tests/test_store.py', part)
        files = sorted(p.name for p in self.cache.iterdir())
        self.assertEqual(len(files), 2)
        # same content -> same files (a parallel agent reuses them)
        context_pack.part_layer(self.folder, tree, self.cache, 'Store loading', ['app/store.py'])
        self.assertEqual(sorted(p.name for p in self.cache.iterdir()), files)
        # a change to an owned file gives a new package
        (self.folder / 'app/store.py').write_text('def load():\n    return 2\n')
        changed = context_pack.part_layer(self.folder, ats.worktree_tree(self.folder), self.cache,
                                          'Store loading', ['app/store.py'])
        self.assertNotIn('class Store:', changed)
        main = context_pack.part_layer(self.folder, tree, self.cache, 'Main', ['app/main.py'])
        self.assertIn('Local modules these files import: app/store.py', main)

    def test_part_without_known_files_is_empty(self):
        tree = ats.worktree_tree(self.folder)
        self.assertEqual(context_pack.part_layer(self.folder, tree, self.cache, 'Something new', []), '')


class KnowledgeSelectionTests(unittest.TestCase):
    def item(self, i, kind, title, observation, created='2026-10-01'):
        return {'id': f'id{i:06d}-x', 'kind': kind, 'title': title, 'observation': observation,
                'evidence': ['review'], 'status': 'approved', 'created_at': created}

    def test_safety_rules_always_then_relevance_within_budget(self):
        records = [self.item(1, 'rule', 'Never commit secrets', 'Tokens stay out of the repository'),
                   self.item(2, 'lesson', 'Retry the HTTP client', 'requests to the payments api need retries'),
                   self.item(3, 'finding', 'CSS tokens', 'use --accent in app.css'),
                   self.item(4, 'lesson', 'Big unrelated note', 'x' * 380)]
        chosen, text = knowledge_select.select(records, 'Add a retry to the http client', ['client.py'], budget=700)
        titles = [c['title'] for c in chosen]
        self.assertEqual(titles[0], 'Never commit secrets')
        self.assertIn('Retry the HTTP client', titles)
        self.assertNotIn('CSS tokens', titles)          # no overlap with the task
        self.assertIn('[K:id000002]', text)
        tiny, _ = knowledge_select.select(records, 'Add a retry to the http client', budget=10)
        self.assertEqual([c['title'] for c in tiny], ['Never commit secrets'])   # safety is never dropped

    def test_finding_cites_a_learning(self):
        findings = ats.normalize_findings([dict(MAJOR, rule='K:id000002'), dict(MAJOR, id='F2', rule='bogus rule')])
        self.assertEqual(findings[0]['rule'], 'K:id000002')
        self.assertNotIn('rule', findings[1])

    def test_selection_is_recorded_once_per_task(self):
        events = []
        with patch.object(ats, 'knowledge_context', return_value='\nLEARNINGS'), \
             patch.object(ats, 'vetted_knowledge', return_value=([self.item(1, 'rule', 'R', 'o')], '\nLEARNINGS')), \
             patch.object(ats, 'record', side_effect=lambda kind, *a, **f: events.append(kind)), \
             patch.object(ats, '_task_knowledge', {}):
            self.assertEqual(ats.task_knowledge('t1', 'task'), '\nLEARNINGS')
            self.assertEqual(ats.task_knowledge('t1', 'task'), '\nLEARNINGS')
        self.assertEqual(events, ['knowledge_selected'])


class ModelPerPhaseTests(unittest.TestCase):
    def tearDown(self):
        usage.reset_context()

    def test_phase_model_but_never_for_review_or_high_risk(self):
        env = {'ATS_MODEL_CLAUDE': 'strong', 'ATS_MODEL_CLAUDE_LEARNING': 'light', 'ATS_MODEL_CLAUDE_REVIEW': 'light'}
        with patch.dict(os.environ, env), patch.object(ats, '_run_flags', {'risky': {'high_risk': True}}):
            usage.set_context(task_id='normal')
            self.assertEqual(ats.selected_agent_model('claude', 'learning'), 'light')
            self.assertEqual(ats.selected_agent_model('claude', 'review'), 'strong')
            self.assertEqual(ats.selected_agent_model('claude', 'implementation'), 'strong')
            usage.set_context(task_id='risky')
            self.assertEqual(ats.selected_agent_model('claude', 'learning'), 'strong')


class CriticalPathTests(unittest.TestCase):
    def test_longest_chain_goes_first(self):
        plan = {'plan_version': 2, 'tasks': [
            {'id': 'side', 'title': 'Independent side documentation task', 'depends_on': [], 'owned_paths': ['docs/']},
            {'id': 'other', 'title': 'Another independent side task', 'depends_on': [], 'owned_paths': ['misc.py']},
            {'id': 'core', 'title': 'Core model that others build on', 'depends_on': [], 'owned_paths': ['core.py']},
            {'id': 'api', 'title': 'API on top of the core model', 'depends_on': ['core'], 'owned_paths': ['api.py']},
            {'id': 'ui', 'title': 'Screen on top of the API layer', 'depends_on': ['api'], 'owned_paths': ['ui.py']}]}
        self.assertEqual(dag.critical_path(plan)['core'], 3)
        first = [n['id'] for n in dag.execution_batches(plan)[0]]
        self.assertIn('core', first)   # without critical path ordering this was ['side', 'other']


class ReviewLoopEfficiencyTests(unittest.TestCase):
    """The review loop with the gate, delta review, deep review and the shared prompt prefix."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self.temp.name)
        make_repo(self.folder, {'app.py': 'x = 1\n'})
        (self.folder / 'app.py').write_text('x = 2\n')
        self.events, self.calls = [], []

    def tearDown(self):
        self.temp.cleanup()
        usage.reset_context()

    def run_loop(self, script, task='task', conf=None, owned=None):
        steps = list(script)

        def fake_invoke(agent, prompt, cwd, quiet=False, **kw):
            self.calls.append((agent, prompt))
            expected, action = steps.pop(0)
            self.assertEqual(agent, expected)
            return action(pathlib.Path(cwd), prompt)

        config = {'test_commands': [PASS], 'max_revisions': 2}
        config.update(conf or {})
        with patch.object(ats, 'invoke', side_effect=fake_invoke), \
             patch.object(ats, 'record', side_effect=lambda kind, summary, task=None, **f:
                          self.events.append(dict(kind=kind, summary=summary, **f))), \
             patch.object(ats, 'capture_learning_proposals'), patch.object(ats, 'codex_preflight'), \
             patch.object(ats, 'knowledge_context', return_value='\nVETTED'), \
             patch.object(ats, 'vetted_knowledge', return_value=([], '')), \
             patch.object(ats, 'state_directory', return_value=self.folder.parent / 'state' / 'data'), \
             patch.object(ats, 'enabled_agents', return_value=['codex', 'claude']), \
             patch.object(ats, 'executable', return_value='x'), \
             patch.object(ats, '_task_knowledge', {}), patch.object(ats, '_repo_context', {}):
            usage.reset_context()
            result = ats.review_loop(task, 'tid', self.folder, 'ats/b', 'codex', ats.UnavailableAgents(),
                                     config, quiet=True, owned_paths=owned)
        self.assertEqual(steps, [], 'not every scripted call happened')
        return result

    def of(self, kind):
        return [e for e in self.events if e['kind'] == kind]

    def test_gate_failure_goes_back_to_the_implementer_before_any_review(self):
        (self.folder / 'app.py').write_text('def broken(:\n')

        def repair(folder, prompt):
            self.assertIn('python syntax', prompt)
            (folder / 'app.py').write_text('x = 3\n')
            return 'repaired'

        approved, *_ = self.run_loop([('codex', repair), ('claude', lambda f, p: APPROVE)])
        self.assertTrue(approved)
        gates = self.of('pre_review_gate')
        self.assertEqual([g['passed'] for g in gates], [False, True])
        self.assertIn('ROLE: independent reviewer', self.calls[1][1])
        self.assertIn('Pre-review gate', self.calls[1][1])

    def test_gate_that_keeps_failing_escalates_with_the_reason(self):
        (self.folder / 'app.py').write_text('def broken(:\n')
        approved, *_ = self.run_loop([('codex', lambda f, p: 'tried')], conf={'max_repairs': 1})
        self.assertFalse(approved)
        reasons = self.of('escalated')[0]['blocking_reasons'][0]
        self.assertTrue(reasons.startswith('stuck:') or 'keep failing' in reasons, reasons)
        self.assertFalse(self.of('review_result'))   # no reviewer tokens spent

    def test_second_round_is_a_delta_review_and_prompts_share_one_prefix(self):
        def first_review(folder, prompt):
            return 'ATS_FINDINGS_JSON: ' + json.dumps([MAJOR]) + '\nVERDICT: CHANGES_REQUESTED'

        def revise(folder, prompt):
            (folder / 'app.py').write_text('x = 2\ny = 1\n')
            return 'ATS_RESPONSES_JSON: [{"id":"F1","status":"fixed","note":"added"}]'

        approved, *_ = self.run_loop([('claude', first_review), ('codex', revise), ('claude', lambda f, p: APPROVE)])
        self.assertTrue(approved)
        results = self.of('review_result')
        self.assertEqual([r['review_mode'] for r in results], ['full', 'delta'])
        second = self.calls[2][1]
        self.assertIn('DELTA RE-REVIEW', second)
        self.assertIn('+y = 1', second)
        self.assertNotIn('-x = 1', second)   # the first change is not sent again
        prefixes = {p[:p.index('ROLE: ')] for _, p in self.calls}
        self.assertEqual(len(prefixes), 1, 'implementation, revision and review prompts start identically')
        prefix = prefixes.pop()
        self.assertIn('VETTED', prefix)
        self.assertIn('Repository map', prefix)
        self.assertNotIn('tid', prefix)

    def test_high_risk_change_needs_the_security_pass_too(self):
        def security(folder, prompt):
            self.assertIn('FOCUSED SECURITY', prompt)
            return ('ATS_FINDINGS_JSON: ' + json.dumps([dict(MAJOR, summary='Token logged')])
                    + '\nVERDICT: CHANGES_REQUESTED')

        approved, *_ = self.run_loop([('claude', lambda f, p: APPROVE), ('claude', security)],
                                     task='Fix the login password check', conf={'max_revisions': 0})
        self.assertFalse(approved)
        result = self.of('review_result')[0]
        self.assertEqual(result['review_level'], 'deep')
        self.assertEqual(result['findings'][0]['id'], 'SF1')
        self.assertTrue(any(r.startswith('security pass') for r in result['blocking_reasons']))

    def test_light_review_for_docs(self):
        (self.folder / 'README.md').write_text('# Docs\n')
        (self.folder / 'app.py').write_text('x = 1\n')
        approved, *_ = self.run_loop([('claude', lambda f, p: APPROVE)])
        self.assertTrue(approved)
        self.assertEqual(self.of('review_result')[0]['review_level'], 'light')
        self.assertIn('LIGHT REVIEW', self.calls[0][1])

    def test_revision_that_claims_a_fix_but_changes_nothing_stops(self):
        approved, *_ = self.run_loop([
            ('claude', lambda f, p: 'ATS_FINDINGS_JSON: ' + json.dumps([MAJOR]) + '\nVERDICT: CHANGES_REQUESTED'),
            ('codex', lambda f, p: 'ATS_RESPONSES_JSON: [{"id":"F1","status":"fixed","note":"done"}]')])
        self.assertFalse(approved)
        self.assertIn('changed no files', ' '.join(self.of('escalated')[0]['blocking_reasons']))

    def test_ownership_drift_is_reported_to_the_reviewer(self):
        (self.folder / 'other.py').write_text('z = 1\n')

        def review(folder, prompt):
            self.assertIn('Changes outside the files this part owns', prompt)
            return APPROVE

        approved, *_ = self.run_loop([('claude', review)], owned=['app.py'])
        self.assertTrue(approved)
        self.assertEqual(self.of('ownership_drift')[0]['paths'], ['other.py'])

    def test_same_provider_review_only_when_the_project_allows_it(self):
        unavailable = ats.UnavailableAgents()
        ats.mark_unavailable(unavailable, 'claude', 'usage')
        for allowed, expect in ((False, False), (True, True)):
            self.events, self.calls = [], []
            steps = [('codex', lambda f, p: APPROVE)] if allowed else []

            def fake_invoke(agent, prompt, cwd, quiet=False, **kw):
                expected, action = steps.pop(0)
                self.assertEqual(agent, expected)
                return action(cwd, prompt)

            with patch.object(ats, 'invoke', side_effect=fake_invoke), \
                 patch.object(ats, 'record', side_effect=lambda kind, summary, task=None, **f:
                              self.events.append(dict(kind=kind, **f))), \
                 patch.object(ats, 'capture_learning_proposals'), patch.object(ats, 'codex_preflight'), \
                 patch.object(ats, 'knowledge_context', return_value=''), \
                 patch.object(ats, 'state_directory', return_value=self.folder.parent / 'state' / 'data'), \
                 patch.object(ats, 'enabled_agents', return_value=['codex', 'claude']), \
                 patch.object(ats, 'executable', return_value='x'):
                usage.reset_context()
                approved, *_ = ats.review_loop('task', 'tid', self.folder, 'ats/b', 'codex', unavailable,
                                               {'test_commands': [PASS], 'max_revisions': 0,
                                                'review_same_provider_fallback': allowed}, quiet=True)
            self.assertEqual(approved, expect)
            self.assertEqual(bool(self.of('review_same_provider')), allowed)

    def test_budget_spent_escalates_before_the_review(self):
        def review(folder, prompt):
            raise AssertionError('no review once the budget is spent')

        with patch.object(budgets, 'multiplier', return_value=1.0):
            budget = budgets.Budget(tokens=10)
            budget.add({'input_tokens': 50})
            usage.reset_context()

            def run():
                with patch.object(ats, 'invoke', side_effect=lambda *a, **k: review(None, None)), \
                     patch.object(ats, 'record', side_effect=lambda kind, summary, task=None, **f:
                                  self.events.append(dict(kind=kind, **f))), \
                     patch.object(ats, 'knowledge_context', return_value=''):
                    usage.set_context(budget=budget)
                    return ats.review_loop('task', 'tid', self.folder, 'ats/b', 'codex', ats.UnavailableAgents(),
                                           {'test_commands': [PASS]}, quiet=True)
            approved, *_ = run()
        self.assertFalse(approved)
        self.assertTrue(self.of('budget_exceeded'))


if __name__ == '__main__':
    unittest.main()
