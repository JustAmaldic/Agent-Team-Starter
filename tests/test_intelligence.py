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
            {"event_type": "run_finished", "task_id": "1", "approved": True,
             "duration_seconds": 12.5, "review_rounds": 1, "outcome": "approved"},
        ]
        metrics = performance(events)
        self.assertTrue(metrics)
        self.assertFalse(all(m["reliable"] for m in metrics))
        self.assertIn("python", categories("Fix Python test"))

    def test_metrics_use_completion_totals_for_success_and_failure(self):
        events = []
        for task_id, approved, rounds, duration in (("1", True, 3, 120.5),
                                                    ("2", False, 2, 60),
                                                    ("3", True, 0, 10)):
            events.extend([
                {"event_type": "assigned", "task_id": task_id, "implementer": "codex",
                 "summary": "Python"},
                {"event_type": "run_finished", "task_id": task_id, "approved": approved,
                 "duration_seconds": duration, "review_rounds": rounds,
                 "outcome": "approved" if approved else "escalated"},
            ])
        # The summary owns the total, even if only part of the review history was loaded.
        events.append({"event_type": "review_result", "task_id": "1"})
        self.assertEqual(performance(events), [
            {"agent": "codex", "category": "python", "tasks": 3, "approved": 2,
             "revisions": 3, "tokens": 0, "duration_seconds": 190.5, "samples": 3,
             "approval_rate": 0.667, "reliable": True},
        ])

    def test_incomplete_runs_and_live_finished_do_not_supply_samples(self):
        events = [
            {"event_type": "assigned", "task_id": "1", "implementer": "codex"},
            {"event_type": "review_result", "task_id": "1", "approved": True},
            {"event_type": "phase", "stage": "finished", "task_id": "1", "approved": True},
            {"event_type": "finished", "task_id": "1", "approved": True},
            {"event_type": "run_finished", "task_id": "2", "approved": True,
             "duration_seconds": 10, "review_rounds": 1, "outcome": "approved"},
        ]
        self.assertEqual(performance(events), [])

    def test_metrics_do_not_join_different_projects(self):
        events = [
            {"project": "one", "event_type": "assigned", "task_id": "1",
             "implementer": "codex"},
            {"project": "two", "event_type": "run_finished", "task_id": "1",
             "approved": True, "duration_seconds": 10, "review_rounds": 1,
             "outcome": "approved"},
        ]
        self.assertEqual(performance(events), [])

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
