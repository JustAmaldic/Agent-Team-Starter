"""Agents get a brief of the project's existing style, and reviewers check consistency."""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats


class StyleContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self.temp.name)
        ats._style_cache.clear()

    def tearDown(self):
        self.temp.cleanup()

    def test_brief_lists_sources_tokens_fonts_and_notes(self):
        web = self.folder / 'app' / 'web'
        web.mkdir(parents=True)
        (web / 'app.css').write_text(':root {\n  --bg: #0A0A0B;\n  --accent: #A07080;\n  --radius: 9px;\n}\n'
                                     'body { font-family: Geist, system-ui; }\n.x { font-family: Geist, system-ui; }\n')
        (self.folder / 'node_modules' / 'lib').mkdir(parents=True)
        (self.folder / 'node_modules' / 'lib' / 'big.css').write_text(':root{' + ''.join(f'--t{i}: 1;' for i in range(80)) + '}')
        (self.folder / '.editorconfig').write_text('root = true\n')
        (self.folder / 'AGENTS.md').write_text('rules\n')
        brief = ats.style_context(self.folder, {'style_guide': ['Dutch UI text', 'No emoji']})
        self.assertIn('MATCH THE EXISTING STYLE', brief)
        self.assertIn('AGENTS.md, .editorconfig', brief)
        self.assertIn('app/web/app.css', brief)   # node_modules is ignored
        self.assertIn('--accent: #A07080', brief)
        self.assertIn('Fonts in use: Geist, system-ui', brief)
        self.assertIn('Dutch UI text\nNo emoji', brief)
        self.assertLessEqual(len(brief), ats.STYLE_LIMIT + 2)

    def test_project_without_style_files_still_gets_the_rule(self):
        brief = ats.style_context(self.folder, {})
        self.assertIn('Reuse existing components', brief)
        self.assertNotIn('Design tokens', brief)

    def test_review_and_revision_prompts_carry_it(self):
        brief = ats.style_context(self.folder, {'style_guide': 'Keep the black and old-rose palette'})
        review = ats.review_prompt('task', [], 'diff', [], brief)
        self.assertIn('consistency with the existing project', review)
        self.assertIn('old-rose palette', review)
        self.assertIn('old-rose palette', ats.revision_prompt('task', 'review', [], brief))


if __name__ == '__main__':
    unittest.main()
