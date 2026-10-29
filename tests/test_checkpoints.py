import tempfile
import unittest
from pathlib import Path

from ats_modules import checkpoints


class CheckpointTests(unittest.TestCase):
    def test_persists_only_for_matching_plan_worktree_and_git_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = ["First reviewed component", "Second reviewed component"]
            worktree = Path(tmp) / "work"
            head = "a" * 40
            checkpoints.save_checkpoint(tmp, "run-123", plan, worktree, head, 1,
                                        [{"subtask": 1, "implementer": "codex", "reviewer": "claude"}])
            loaded = checkpoints.load_checkpoint(tmp, "run-123", plan, worktree, head)
            self.assertEqual(loaded["completed"], 1)
            self.assertIsNone(checkpoints.load_checkpoint(tmp, "run-123", plan, worktree, "b" * 40))
            self.assertIsNone(checkpoints.load_checkpoint(tmp, "run-123", list(reversed(plan)), worktree, head))
            self.assertIsNone(checkpoints.load_checkpoint(tmp, "run-123", plan, Path(tmp) / "other", head))

    def test_rejects_invalid_sha_and_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                checkpoints.save_checkpoint(tmp, "../escape", ["a"], tmp, "a" * 40, 0, [])
            with self.assertRaises(ValueError):
                checkpoints.save_checkpoint(tmp, "safe", ["a"], tmp, "short", 0, [])


if __name__ == "__main__":
    unittest.main()
