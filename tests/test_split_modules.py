"""t67: ats.py is split into modules without changing behaviour."""
import pathlib
import re
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats
from ats_contract import valid_phase
from ats_modules import part_flow, review_flow, task_flow

ROOT = pathlib.Path(__file__).resolve().parents[1]
MOVED = (review_flow, part_flow, task_flow)


class SplitTests(unittest.TestCase):
    def test_ats_py_stays_small(self):
        self.assertLess((ROOT / 'ats.py').read_text(encoding='utf-8').count('\n'), 2000)

    def test_every_phase_in_every_module_is_in_the_contract(self):
        sources = [ROOT / 'ats.py', *[pathlib.Path(m.__file__) for m in MOVED]]
        used = set()
        for path in sources:
            used |= set(re.findall(r"phase\('([a-z_]+)'", path.read_text(encoding='utf-8')))
        self.assertTrue({'update', 'checks', 'revision', 'merge', 'parallel'} <= used)
        self.assertEqual({p for p in used if not valid_phase(p)}, set())

    def test_moved_names_are_the_same_objects_on_ats(self):
        for module in MOVED:
            for name in module.__all__:
                self.assertIs(getattr(ats, name), getattr(module, name), name)

    def test_moved_code_uses_patches_on_ats(self):
        from unittest.mock import patch
        with patch.object(ats, 'plan_waves', return_value=None):
            self.assertFalse(ats.valid_plan(['a part', 'another part']))

    def test_running_as_a_script_uses_one_module(self):
        # `python ats.py` runs as __main__; the moved modules `import ats` and must get that same module.
        code = ("import runpy, sys; sys.argv = ['ats.py', '--help']\n"
                "try:\n    runpy.run_path('ats.py', run_name='__main__')\nexcept SystemExit:\n    pass\n"
                "import ats_modules.review_flow as r; print(r.ats.__name__)")
        out = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.strip().endswith('__main__'), out.stdout[-300:])


if __name__ == '__main__':
    unittest.main()
