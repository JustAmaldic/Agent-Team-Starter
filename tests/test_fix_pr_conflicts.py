"""Resolving the merge conflicts of an open pull request keeps what both sides added."""
import argparse
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats
from ats_modules import merge_guard

PASS = [sys.executable, '-c', 'pass']
START = 'def shared():\n    return 1\n\n\ndef old_helper():\n    return 0\n'
ON_MAIN = 'def shared():\n    return 2\n\n\ndef main_feature():\n    return "main"\n'          # removes old_helper on purpose
ON_PR = 'def shared():\n    return 3\n\n\ndef old_helper():\n    return 0\n\n\ndef pr_feature():\n    return "pr"\n'
BOTH = 'def shared():\n    return 5\n\n\ndef main_feature():\n    return "main"\n\n\ndef pr_feature():\n    return "pr"\n'


def git(*args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class MergeGuardTests(unittest.TestCase):
    def test_name_added_by_one_side_must_survive(self):
        self.assertEqual(merge_guard.lost(START, ON_PR, ON_MAIN, ON_MAIN), ['pr_feature'])
        self.assertEqual(merge_guard.lost(START, ON_PR, ON_MAIN, ON_PR), ['main_feature'])

    def test_combined_resolution_loses_nothing_and_purposeful_removal_is_fine(self):
        self.assertEqual(merge_guard.lost(START, ON_PR, ON_MAIN, BOTH), [])   # old_helper: removed on main on purpose

    def test_js_routes_tests_and_ids(self):
        ours = "export function renderInbox() {}\nconst PAGE = 1;\nit('shows the inbox', () => {});\n<div id=\"inbox-list\">"
        theirs = "@app.get('/api/inbox')\ndef inbox():\n    pass\n"
        self.assertEqual(merge_guard.lost('', ours, theirs, ''),
                         ['/api/inbox', 'PAGE', 'inbox', 'inbox-list', 'renderInbox', 'shows the inbox'])

    def test_only_code_files_and_deleted_file(self):
        found = merge_guard.check({'notes.md': ('', 'def a(): pass', '', ''),
                                   'a.py': (START, ON_PR, ON_MAIN, None)})
        self.assertEqual(found, [{'file': 'a.py', 'lost': ['(the whole file)']}])
        self.assertIn('BLOCKER', merge_guard.note(found))
        self.assertEqual(merge_guard.note([]), '')


class FixPullRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.temp.name)
        self.origin, self.other, self.app = base / 'origin.git', base / 'other', base / 'app'
        git('init', '-q', '--bare', '-b', 'main', str(self.origin), cwd=base)
        git('clone', '-q', str(self.origin), str(self.other), cwd=base)
        git('config', 'user.name', 't', cwd=self.other)
        git('config', 'user.email', 't@t', cwd=self.other)
        self.commit(START, 'init', 'main')
        git('checkout', '-q', '-b', 'feature', cwd=self.other)
        self.commit(ON_PR, 'pr work', 'feature')
        git('checkout', '-q', 'main', cwd=self.other)
        self.commit(ON_MAIN, 'main moved', 'main')
        git('clone', '-q', str(self.origin), str(self.app), cwd=base)
        git('config', 'user.name', 't', cwd=self.app)
        git('config', 'user.email', 't@t', cwd=self.app)
        self.events = []
        self.patches = [patch.object(ats, 'APP', self.app), patch.object(ats, 'phase'), patch('builtins.print'),
                        patch.object(ats, 'record', side_effect=lambda kind, summary, task=None, **f: self.events.append(dict(kind=kind, summary=summary, **f)))]
        for item in self.patches:
            item.start()
        self.pr = {'number': 7, 'head': 'feature', 'base': 'main', 'url': 'https://github.com/o/r/pull/7'}
        self.folder = base / 'app-ats-1'
        git('fetch', '-q', 'origin', cwd=self.app)
        git('worktree', 'add', '-q', '-b', 'ats/x', str(self.folder), 'origin/feature', cwd=self.app)

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    def commit(self, text, message, branch):
        (self.other / 'app.py').write_text(text)
        git('add', '-A', cwd=self.other)
        git('commit', '-q', '-m', message, cwd=self.other)
        git('push', '-q', 'origin', branch, cwd=self.other)

    def run_fix(self, resolved, approved=True, push=True):
        prompts = []

        def resolve(agent, prompt, cwd, unavailable, task_id, purpose, quiet=False, fallback=True):
            prompts.append(prompt)
            (pathlib.Path(cwd) / 'app.py').write_text(resolved)
            return agent, 'resolved'

        with patch.object(ats, 'invoke_with_fallback', side_effect=resolve), \
             patch.object(ats, 'shared_prefix', return_value=''), \
             patch.object(ats, 'review_loop', return_value=(approved, 'claude', 'codex', [{'cycle': 0}])) as review:
            outcome = ats.fix_pr_conflicts('Fix PR 7', 'tid', self.folder, 'ats/x', {'test_commands': [PASS]}, self.pr,
                                           'codex', ats.UnavailableAgents(), push)
        return outcome, prompts, review

    def origin_file(self, branch='feature'):
        return git('show', f'{branch}:app.py', cwd=self.origin) + '\n'

    def test_approved_resolution_updates_the_pull_request_branch(self):
        before = git('rev-parse', 'feature', cwd=self.origin)
        outcome, prompts, review = self.run_fix(BOTH)
        self.assertEqual(outcome, 'approved')
        self.assertIn('KEEP THE FUNCTIONALITY OF BOTH SIDES', prompts[0])
        self.assertIn('pull request #7', prompts[0])
        self.assertEqual(self.origin_file(), BOTH)
        git('merge-base', '--is-ancestor', 'main', 'feature', cwd=self.origin)      # the PR now merges cleanly
        git('merge-base', '--is-ancestor', before, 'feature', cwd=self.origin)      # fast-forward: PR history kept
        self.assertEqual(self.origin_file('main'), ON_MAIN)                          # main itself is never touched
        guard = next(e for e in self.events if e['kind'] == 'merge_guard')
        self.assertEqual(guard['files'], [])
        self.assertEqual(review.call_args.kwargs['earlier_review_notes'], '')
        self.assertTrue(any(e['kind'] == 'pr_conflicts_resolved' for e in self.events))

    def test_resolution_that_drops_one_side_is_reported_to_the_reviewer(self):
        outcome, _, review = self.run_fix(ON_MAIN.replace('return 2', 'return 5'), approved=False)
        self.assertEqual(outcome, 'escalated')
        guard = next(e for e in self.events if e['kind'] == 'merge_guard')
        self.assertEqual(guard['files'], [{'file': 'app.py', 'lost': ['pr_feature']}])
        self.assertIn('pr_feature', review.call_args.kwargs['earlier_review_notes'])
        self.assertEqual(self.origin_file(), ON_PR)                                  # rejected: the PR is unchanged

    def test_pull_request_that_moved_meanwhile_is_never_overwritten(self):
        def resolve_and_move(agent, prompt, cwd, unavailable, task_id, purpose, quiet=False, fallback=True):
            (pathlib.Path(cwd) / 'app.py').write_text(BOTH)
            git('checkout', '-q', 'feature', cwd=self.other)
            self.commit(ON_PR + '\n# someone pushed\n', 'more pr work', 'feature')
            return agent, 'resolved'

        with patch.object(ats, 'invoke_with_fallback', side_effect=resolve_and_move), \
             patch.object(ats, 'shared_prefix', return_value=''), \
             patch.object(ats, 'review_loop', return_value=(True, 'claude', 'codex', [{'cycle': 0}])):
            outcome = ats.fix_pr_conflicts('Fix PR 7', 'tid', self.folder, 'ats/x', {'test_commands': [PASS]}, self.pr,
                                           'codex', ats.UnavailableAgents(), True)
        self.assertEqual(outcome, 'escalated')
        self.assertIn('# someone pushed', self.origin_file())
        self.assertIn('changed meanwhile', self.events[-1]['summary'])

    def test_without_push_the_pull_request_is_not_touched(self):
        outcome, _, _ = self.run_fix(BOTH, push=False)
        self.assertEqual(outcome, 'approved')
        self.assertEqual(self.origin_file(), ON_PR)

    def test_pull_request_without_conflict_is_blocked(self):
        git('checkout', '-q', 'feature', cwd=self.other)
        git('merge', '-q', '-X', 'ours', '--no-edit', 'main', cwd=self.other)
        git('push', '-q', 'origin', 'feature', cwd=self.other)
        git('fetch', '-q', 'origin', cwd=self.app)
        git('reset', '-q', '--hard', 'origin/feature', cwd=self.folder)
        outcome, prompts, _ = self.run_fix(BOTH)
        self.assertEqual((outcome, prompts), ('blocked', []))


class ArgumentTests(unittest.TestCase):
    def args(self, **values):
        return argparse.Namespace(**{'fix_pr': 7, 'pr_head': 'feature/x', 'pr_base': 'main', 'pr_url': None, **values})

    def test_valid_and_invalid_arguments(self):
        self.assertEqual(ats.pull_request_argument(self.args(pr_url='https://github.com/o/r/pull/7')),
                         {'number': 7, 'head': 'feature/x', 'base': 'main', 'url': 'https://github.com/o/r/pull/7'})
        self.assertIsNone(ats.pull_request_argument(self.args(fix_pr=None)))
        self.assertIsNone(ats.pull_request_argument(self.args(pr_url='https://evil.example/x'))['url'])
        for bad in ({'pr_head': '--upload-pack=x'}, {'pr_head': 'a..b'}, {'pr_base': None}, {'pr_head': 'a b'}):
            with self.assertRaises(RuntimeError):
                ats.pull_request_argument(self.args(**bad))


if __name__ == '__main__':
    unittest.main()
