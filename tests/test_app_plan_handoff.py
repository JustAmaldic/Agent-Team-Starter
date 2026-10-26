import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import ats


class AppTaskPlanTests(unittest.TestCase):
    def test_user_edited_plan_is_used_without_replanning(self):
        waves = ["Inspect ARC inbox integration", [
            "Add spoofing detection with tests in mail_security.py",
            "Add weekly newsletter audit with tests in privacy_audit.py",
        ], "Integrate morning briefing and test end-to-end"]
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "plan.json"
            path.write_text(json.dumps({"task": "Improve ARC inbox", "waves": waves}), encoding="utf-8")
            self.assertEqual(ats.provided_task_plan(path, "Improve ARC inbox"), waves)
            self.assertEqual([len(w) for w in ats.plan_waves(waves)], [1, 2, 1])

    def test_plan_is_bound_to_exact_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "plan.json"
            path.write_text(json.dumps({"task": "Other task",
                "waves": ["Implement separate module A", "Implement separate module B"]}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "bound to"):
                ats.provided_task_plan(path, "Improve ARC inbox")

    def test_reject_invalid_or_oversized_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "plan.json"
            path.write_text(json.dumps({"task": "Improve ARC inbox",
                "waves": [["One", "Two", "Three"]]}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Invalid custom task plan"):
                ats.provided_task_plan(path, "Improve ARC inbox")
            path.write_text(" " * 32769, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "too large"):
                ats.provided_task_plan(path, "Improve ARC inbox")

    def test_preview_runs_existing_preflight_planner_without_implementation(self):
        waves = ["Inspect existing ARC Gmail APIs", [
            "Implement sender risk scoring with tests", "Implement newsletter inventory with tests"
        ], "Integrate both features and regression tests"]
        with tempfile.TemporaryDirectory() as tmp:
            fake_folder = pathlib.Path(tmp) / "application-ats-plan"
            with patch.object(ats, "APP", pathlib.Path(tmp)), \
                 patch.object(ats, "execute") as execute, \
                 patch.object(ats, "enabled_agents", return_value=["codex", "claude"]), \
                 patch.object(ats, "select", return_value="codex"), \
                 patch.object(ats, "preplan_task", return_value=waves) as preplan, \
                 patch.object(ats, "make_local_workspace"), \
                 patch.object(ats.shutil, "rmtree"):
                from subprocess import CompletedProcess
                execute.return_value = CompletedProcess([], 128, "", "")
                result = ats.preview_task_plan("Improve ARC inbox", agent="auto")
                self.assertEqual(result, waves)
                preplan.assert_called_once()


if __name__ == "__main__":
    unittest.main()
