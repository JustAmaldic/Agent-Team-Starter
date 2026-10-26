import unittest

from ats_intelligence import categories, performance, pr_feedback_items, relation_issues, risk_profile


class IntelligenceTests(unittest.TestCase):
    def test_risk_requires_approval_for_authentication(self):
        result = risk_profile("Change OAuth login flow")
        self.assertEqual(result["level"], "high")
        self.assertTrue(result["require_human_approval"])
        self.assertTrue(result["security_review"])

    def test_low_risk_is_not_blocked(self):
        self.assertEqual(risk_profile("Adjust CSS button spacing")["level"], "low")

    def test_metrics_require_enough_samples(self):
        events = [
            {"event_type": "assigned", "task_id": "1", "implementer": "codex",
             "summary": "Fix Python test"},
            {"event_type": "review_result", "task_id": "1", "approved": True},
            {"event_type": "finished", "task_id": "1", "approved": True},
        ]
        metrics = performance(events)
        self.assertTrue(metrics)
        self.assertFalse(all(m["reliable"] for m in metrics))
        self.assertIn("python", categories("Fix Python test"))

    def test_feedback_is_deduplicated(self):
        comments = [{"id": 7, "body": "Please fix this"}, {"id": 7, "body": "Duplicate"}]
        self.assertEqual(len(pr_feedback_items(comments)), 1)
        self.assertEqual(pr_feedback_items(comments, ["github-pr-comment:7"]), [])

    def test_knowledge_conflicts_require_review(self):
        items = [{"id": "a"}, {"id": "b"}]
        results = relation_issues(items, [{"source": "a", "target": "b", "kind": "contradicts"}])
        self.assertEqual(results[0]["type"], "conflict")
        self.assertTrue(results[0]["needs_human"])


if __name__ == "__main__":
    unittest.main()
