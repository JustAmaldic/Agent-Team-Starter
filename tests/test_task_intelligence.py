import unittest
from ats_modules.task_intelligence import classify_task, suggest_plan, validate_plan


class TaskIntelligenceTests(unittest.TestCase):
    def test_sensitive_tasks_require_human_review(self):
        result = classify_task("Migrate the OAuth database and production keys")
        self.assertEqual(result["level"], "high")
        self.assertTrue(result["human_gate"])
        self.assertIn("security", result["risks"])
        self.assertIn("database", result["risks"])
        self.assertFalse(result["auto_approve_knowledge"])

    def test_simple_task_does_not_trigger_high_risk(self):
        result = classify_task("Polish a CSS margin")
        self.assertFalse(result["human_gate"])

    def test_plan_is_editable_and_dependencies_are_ordered(self):
        result = suggest_plan("Refactor architecture of Python application")
        self.assertTrue(validate_plan(result))
        self.assertEqual(result["steps"][-1]["depends_on"], ["implementation"])
        result["steps"][0]["title"] = "Inspect the relevant existing modules"
        self.assertTrue(validate_plan(result))

    def test_invalid_dependent_and_duplicate_steps_rejected(self):
        plan = suggest_plan("Write documentation")
        plan["steps"][0]["depends_on"] = ["verification"]
        with self.assertRaisesRegex(ValueError, "preceding"):
            validate_plan(plan)
        plan = suggest_plan("Write documentation")
        plan["steps"][1]["id"] = "scope"
        with self.assertRaisesRegex(ValueError, "unique"):
            validate_plan(plan)


if __name__ == "__main__":
    unittest.main()
