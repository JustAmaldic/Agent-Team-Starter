"""Opt-in solo mode keeps review independent and worktree safety intact."""
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import ats


class SoloPlanningTests(unittest.TestCase):
    def test_single_agent_can_plan_only_when_enabled(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = pathlib.Path(temp)
            with patch.object(ats, "enabled_agents", return_value=["codex"]), \
                 patch.object(ats, "phase"), \
                 patch.object(ats, "index_tree", return_value="unchanged"), \
                 patch.object(ats, "restore_if_changed", return_value=[]), \
                 patch.object(ats, "record"), \
                 patch.object(ats, "invoke_with_fallback", return_value=("codex",
                     'ATS_SUBTASKS_JSON: [["Build first independent module", "Build second independent module"]]')):
                with self.assertRaisesRegex(RuntimeError, "requires two"):
                    ats.preplan_task("Build two independently reviewable features with tests",
                                     "run", folder, "codex", set(), {}, mode="always")
                planned = ats.preplan_task("Build two independently reviewable features with tests",
                                            "run", folder, "codex", set(),
                                            {"solo_agent_mode": True}, mode="always")
        self.assertEqual(len(ats.plan_waves(planned)[0]), 2)

    def test_solo_parallel_uses_the_same_provider_in_separate_worktrees(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = pathlib.Path(temp)
            calls = []
            events = []

            def invoke_part(*args, **kwargs):
                calls.append((args[2], args[8]))
                return True, "codex", "codex", []

            def fake_execute(command, cwd=None, check=True, **kwargs):
                import subprocess
                return subprocess.CompletedProcess(command, 0, "abc123\n", "")

            with patch.object(ats, "enabled_agents", return_value=["codex"]), \
                 patch.object(ats, "run_part", side_effect=invoke_part), \
                 patch.object(ats, "execute", side_effect=fake_execute), \
                 patch.object(ats, "phase"), \
                 patch.object(ats, "record", side_effect=lambda *a, **kw: events.append(a[0])), \
                 patch.object(ats, "commit_all"), \
                 patch.object(ats, "merge_parallel_part", return_value=(True, "codex", [])), \
                 patch.object(ats.checkpoints, "save_checkpoint"):
                approved, reviewer, author, cycles = ats.subtask_flow(
                    "Two independent changes", [["Build module A", "Build module B"]],
                    "run", folder, "ats/test", "codex", ats.UnavailableAgents(),
                    {"solo_agent_mode": True}, quiet=True)
        self.assertTrue(approved)
        self.assertEqual(len(calls), 2)
        self.assertEqual([name for _, name in calls], ["codex", "codex"])
        self.assertIn("subtasks_completed", events)

    def test_solo_disabled_preserves_two_agent_requirement(self):
        with patch.object(ats, "enabled_agents", return_value=["codex"]), \
             patch.object(ats, "record"), \
             patch.object(ats, "run_part") as worker:
            result = ats.subtask_flow("task", ["one", "two"], "run", pathlib.Path("."),
                                      "ats/test", "codex", ats.UnavailableAgents(), {})
        self.assertFalse(result[0])
        worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
