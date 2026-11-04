"""Style regressions are checked against existing project evidence, not a universal theme."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ats
from ats_modules import dag, gate, style_guard


class StyleGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'web').mkdir()
        (self.root / 'web/base.css').write_text(':root { --bg: #111111; }\n.input { color: #111111; height: 40px; font-family: Geist; }\n')

    def change(self, line, path='web/new.css'):
        (self.root / path).write_text(line + '\n')
        return f'diff --git a/{path} b/{path}\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+{line}\n'

    def test_new_literals_fail_but_existing_scale_and_tokens_pass(self):
        diff = self.change('.new { color: #ff0000; height: 51px; font-family: Arial; }')
        result = style_guard.check(self.root, diff, {})
        self.assertEqual(result['status'], 'failed')
        for label in ('[color]', '[size]', '[font]', 'web/new.css:1'):
            self.assertIn(label, result['detail'])
        diff = self.change('.new { color: var(--bg); height: 40px; font-family: Geist; }')
        self.assertEqual(style_guard.check(self.root, diff, {})['status'], 'passed')

    def test_existing_declarations_can_move_and_token_definitions_can_grow(self):
        line = '.new { color: #ff0000; height: 51px; }'
        diff = self.change(line).replace('@@ -0,0 +1 @@', '@@ -1 +1 @@\n-' + line)
        self.assertEqual(style_guard.check(self.root, diff, {})['status'], 'passed')
        diff = self.change(':root { --new-accent: #ff0000; --control-height: 51px; }')
        self.assertEqual(style_guard.check(self.root, diff, {})['status'], 'passed')

    def test_inline_requires_scoped_reason_and_exception_is_visible(self):
        diff = self.change("const x = h('div', { style: { left: x } });", 'web/ui.js')
        config = {'style_check_exceptions': [{'path': 'web/ui.js', 'rule': 'inline', 'reason': ''}]}
        self.assertEqual(style_guard.check(self.root, diff, config)['status'], 'failed')
        config['style_check_exceptions'][0]['reason'] = 'Dynamic geometry for drag position'
        result = style_guard.check(self.root, diff, config)
        self.assertEqual(result['status'], 'passed')
        self.assertIn('Dynamic geometry', result['detail'])
        config['style_check_exceptions'][0]['path'] = 'web/other.js'
        self.assertEqual(style_guard.check(self.root, diff, config)['status'], 'failed')

    def test_disabled_non_ui_and_test_fixtures_are_skipped(self):
        diff = self.change('.x { color: #f00; }')
        self.assertEqual(style_guard.check(self.root, diff, {'style_check': False})['status'], 'skipped')
        (self.root / 'tests').mkdir()
        diff = self.change('.x { color: #f00; }', 'tests/fixture.css')
        self.assertEqual(style_guard.check(self.root, diff, {})['status'], 'skipped')

    def test_design_and_inventory_refresh_in_shared_implementer_reviewer_context(self):
        design = self.root / 'DESIGN.md'
        design.write_text('Reuse .input for controls; Dutch labels.')
        first = ats.style_context(self.root, {})
        self.assertIn('Reuse .input', first)
        self.assertIn('web/base.css: classes .input', first)
        design.write_text('Reuse .input and .select; never invent control sizes.')
        second = ats.style_context(self.root, {})
        self.assertIn('never invent control sizes', second)
        self.assertNotEqual(first, second)
        self.assertIn(second, ats.review_prompt('task', [], 'diff', [], second))
        self.assertIn('Keep the palette', ats.style_context(self.root, {'style_guide': ['Keep the palette']}))

    def test_inventory_without_design_ignores_vendor_and_external_symlinks(self):
        (self.root / 'node_modules').mkdir()
        (self.root / 'node_modules/vendor.css').write_text('.invented { color: red; }')
        with tempfile.TemporaryDirectory() as external:
            secret = Path(external) / 'private.css'
            secret.write_text('.private { color: red; }')
            (self.root / 'web/link.css').symlink_to(secret)
            brief = style_guard.brief(self.root)
        self.assertIn('.input', brief)
        self.assertNotIn('invented', brief)
        self.assertNotIn('private', brief)

    def test_reference_is_preserved_and_must_be_an_existing_safe_file(self):
        plan = {'plan_version': 2, 'tasks': [
            {'id': 'ui', 'title': 'Build dropdown component', 'owned_paths': ['web/ui.js'],
             'reference_screen': 'web/base.css'},
            {'id': 'tests', 'title': 'Test dropdown component', 'owned_paths': ['tests/']}]}
        normalized = dag.validate_dag(plan)
        self.assertEqual(normalized['tasks'][0]['reference_screen'], 'web/base.css')
        self.assertIn('Read this file', style_guard.reference_context(self.root, normalized['tasks'][0]))
        for path in ('../secret', '/tmp/secret', 'C:/secret', 'web/missing.js'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                style_guard.reference_context(self.root, {'reference_screen': path})
        plan['tasks'][0]['reference_screen'] = '../secret'
        with self.assertRaises(ValueError):
            dag.validate_dag(plan)

    def test_comments_do_not_create_style_failures(self):
        lines = '/*\n.old { color: #f00; height: 99px; }\n*/\n.x { color: var(--bg); }'
        (self.root / 'web/new.css').write_text(lines)
        diff = '+++ b/web/new.css\n@@ -0,0 +1,4 @@\n' + '\n'.join('+' + row for row in lines.splitlines())
        self.assertEqual(style_guard.check(self.root, diff, {})['status'], 'passed')
        diff = self.change('// style: { left: x }', 'web/ui.js')
        self.assertEqual(style_guard.check(self.root, diff, {})['status'], 'passed')
        # A quoted URL must not hide a real inline declaration following it.
        diff = self.change('const url = "https://example.invalid"; const x = { style: { left: x } };', 'web/ui.js')
        self.assertEqual(style_guard.check(self.root, diff, {})['status'], 'failed')

    def test_style_failure_stops_targeted_tests_in_the_gate(self):
        diff = self.change('.new { color: #ff0000; }')
        with patch.object(gate, 'targeted_tests') as tests:
            results = gate.run_gate({}, self.root, diff, [], lambda *a, **k: None)
        tests.assert_not_called()
        self.assertIn('style consistency', [r['name'] for r in gate.failed(results)])

    def test_learning_prompt_turns_style_findings_into_project_rules(self):
        history = [{'cycle': 0, 'findings': [{'id': 'F1', 'severity': 'MINOR', 'file': 'web/ui.js',
                    'line': '1', 'summary': 'Dropdown styling differs from .input'}]}]
        with patch.object(ats, 'known_titles', return_value=set()), \
             patch.object(ats, 'index_tree', return_value='tree'), \
             patch.object(ats, 'restore_if_changed', return_value=[]), \
             patch.object(ats, 'phase'), patch.object(ats, 'capture_learning_proposals', return_value=[]), \
             patch.object(ats, 'invoke_with_fallback', return_value=('claude', 'ATS_LEARNINGS_JSON: []')) as invoke:
            ats.distill_learnings('UI task', 'tid', self.root, history, 'claude', ats.UnavailableAgents())
        prompt = invoke.call_args.args[1]
        self.assertIn('actionable project-scoped rule', prompt)
        self.assertIn('later independent review confirms', prompt)
        self.assertIn('Dropdown styling differs', prompt)


if __name__ == '__main__':
    unittest.main()
