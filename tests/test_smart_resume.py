import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import ats
from ats_modules import checkpoints


def graph():
    return {"plan_version": 2, "tasks": [
        {"id": "contract", "title": "Define common safety contracts", "owned_paths": ["contracts/"], "depends_on": []},
        {"id": "scoring", "title": "Build phishing scoring with tests", "owned_paths": ["phish.py"], "depends_on": ["contract"]},
        {"id": "newsletter", "title": "Build newsletter evidence classifier", "owned_paths": ["newsletter.py"], "depends_on": ["contract"]},
        {"id": "integrate", "title": "Integrate reviewed components safely", "owned_paths": ["mail_assist.py"], "depends_on": ["scoring", "newsletter"]},
    ]}


class SmartResumeTests(unittest.TestCase):
    def test_reuses_verified_dag_prefix_only(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "worktree"
            directory.mkdir()
            plan = graph()
            head = "a" * 40
            approved = [{"subtask": 1, "implementer": "codex", "reviewer": "claude"}]
            checkpoints.save_checkpoint(temp, "prior-run", plan, directory, head, 1, approved)
            state = checkpoints.verified_resume(temp, "prior-run", plan, directory, head)
            self.assertEqual(state["completed_nodes"], ["contract"])
            self.assertEqual(state["completed"], 1)
            self.assertIsNone(checkpoints.verified_resume(temp, "prior-run", plan, directory, "b" * 40))
            changed = graph()
            changed["tasks"][1]["title"] = "A changed security scoring implementation"
            self.assertIsNone(checkpoints.verified_resume(temp, "prior-run", changed, directory, head))

    def test_partial_pair_does_not_claim_unmerged_node_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "root"
            directory.mkdir()
            plan = graph()
            attribution = [{"subtask": 1, "implementer": "codex", "reviewer": "claude"}]
            pending = [{
                "subtask": 3, "worktree": str(directory), "commit": "b" * 40,
                "wave_base": "a" * 40, "implementer": "claude", "reviewer": "codex"
            }]
            checkpoints.save_checkpoint(temp, "source", plan, directory, "a" * 40,
                                        1, attribution, pending=pending)
            state = checkpoints.verified_resume(temp, "source", plan, directory, "a" * 40)
            self.assertEqual(state["completed_nodes"], ["contract"])
            self.assertEqual(state["pending"][0]["subtask"], 3)

    def test_skip_checkpointed_sequential_node_without_agent_call(self):
        plan = ["Implement shared data contracts", "Repair remaining front-end tests"]
        prior = {"completed": 1,
                 "attribution": [{"subtask": 1, "implementer": "codex", "reviewer": "claude"}]}
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(ats, "run_part", return_value=(True, "codex", "claude", [])) as part, \
             patch.object(ats, "commit_all"), \
             patch.object(ats.checkpoints, "save_checkpoint"), \
             patch.object(ats, "execute", return_value=SimpleNamespace(stdout="a" * 40)), \
             patch.object(ats, "record") as record:
            ok, reviewer, implementer, history = ats.subtask_flow(
                "Repair ARC", plan, "retry-id", Path(temp), "ats/test", "codex",
                set(), {}, resumed=prior)
            self.assertTrue(ok)
            self.assertEqual(part.call_count, 1)
            self.assertEqual(part.call_args.args[2], 2)
            self.assertTrue(part.call_args.kwargs["resume_work"])
            self.assertTrue(any(c.args[0] == "subtask_resumed" for c in record.call_args_list))


if __name__ == "__main__":
    unittest.main()
