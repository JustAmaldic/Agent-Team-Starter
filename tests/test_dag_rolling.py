"""t49: rolling DAG execution; the task worktree does not wait for the side part (no wave barrier)."""
import pathlib
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ats
from ats_modules import dag_rolling
import test_parallel_parts as parallel_parts
from test_parallel_parts import APPROVE, REJECT


def plan():
    return {"plan_version": 2, "tasks": [
        {"id": "slow", "title": "Slow side feature in a.py", "owned_paths": ["a.py"]},
        {"id": "base", "title": "Quick base helper in b.py", "owned_paths": ["b.py"]},
        {"id": "user", "title": "User of the base helper in c.py", "depends_on": ["base"], "owned_paths": ["c.py"]},
    ]}


class PipelineTests(unittest.TestCase):
    def test_main_continues_while_side_runs_and_side_waits_for_merge(self):
        pipe = dag_rolling.Pipeline(plan())
        self.assertEqual(pipe.next_for("main")["id"], "base")      # longest chain first
        pipe.start("main", "base")
        self.assertEqual(pipe.next_for("side")["id"], "slow")
        pipe.start("side", "slow")
        self.assertIsNone(pipe.next_for("main"))                    # user needs base merged
        pipe.finished("main")
        self.assertEqual(pipe.next_for("main")["id"], "user")      # no barrier: slow is still running
        pipe.start("main", "user")
        pipe.finished("side")
        self.assertFalse(pipe.merge_ready())                        # main is mid-part
        pipe.finished("main")
        self.assertTrue(pipe.merge_ready())
        pipe.merged_side()
        self.assertTrue(pipe.done())

    def test_overlapping_files_and_unmerged_dependencies_wait(self):
        p = {"plan_version": 2, "tasks": [
            {"id": "one", "title": "First part touching shared", "owned_paths": ["src/"]},
            {"id": "two", "title": "Second part also in shared", "owned_paths": ["src/x.py"]},
            {"id": "three", "title": "Third part after the second", "depends_on": ["two"], "owned_paths": ["d.py"]}]}
        pipe = dag_rolling.Pipeline(p)
        pipe.start("side", "two")
        self.assertIsNone(pipe.next_for("main"))                    # one overlaps src/x.py, three needs two
        pipe.finished("side")
        self.assertIsNone(pipe.next_for("main"))                    # reviewed is not merged: still locked
        self.assertTrue(pipe.merge_ready())
        pipe.merged_side()
        self.assertEqual(pipe.next_for("main")["id"], "one")
        pipe.start("main", "one")
        self.assertEqual(pipe.next_for("side")["id"], "three")


class RollingFlowTests(parallel_parts.ParallelPartsTests):
    def run_rolling(self, implement, review=lambda agent, prompt: APPROVE):
        original = ats.subtask_flow

        def flow(*args, **kwargs):
            args = list(args)
            args[7] = {**args[7], 'dag_rolling': True}
            return original(*args, **kwargs)
        with patch.object(ats, 'subtask_flow', side_effect=flow):
            return self.run_flow(plan(), implement, review)

    def test_dependent_part_starts_before_the_slow_side_part_finishes(self):
        times = {}

        def implement(agent, cwd, prompt):
            name = next(n for n in ('a.py', 'b.py', 'c.py') if n in prompt.split('Subtask')[1].split('\n')[0])
            times[name] = [time.monotonic()]
            if name == 'a.py':
                time.sleep(1.5)
            (cwd / name).write_text(f'{name[0]} = 1\n')
            times[name].append(time.monotonic())

        approved, *_ = self.run_rolling(implement)
        self.assertTrue(approved)
        self.assertLess(times['c.py'][0], times['a.py'][1])         # no wave barrier
        for name in ('a.py', 'b.py', 'c.py'):
            self.assertTrue((self.folder / name).exists(), name)
        self.assertIn('dag_rolling', self.kinds())
        self.assertIn('subtask_merged', self.kinds())
        self.assertEqual(self.git('worktree', 'list').count('\n'), 1)   # side worktree removed

    def test_a_rejected_part_stops_and_keeps_worktrees(self):
        def implement(agent, cwd, prompt):
            for name in ('a.py', 'b.py', 'c.py'):
                if name in prompt.split('Subtask')[1].split('\n')[0]:
                    (cwd / name).write_text('x = 1\n')

        def review(agent, prompt):
            return REJECT if 'a.py' in prompt.split('Subtask')[1].split('\n')[0] else APPROVE
        approved, *_ = self.run_rolling(implement, review)
        self.assertFalse(approved)
        self.assertNotIn('dag_rolling', self.kinds())


for _name in dir(parallel_parts.ParallelPartsTests):   # run only the rolling tests here
    if _name.startswith('test_') and _name not in RollingFlowTests.__dict__:
        setattr(RollingFlowTests, _name, None)


if __name__ == '__main__':
    unittest.main()
