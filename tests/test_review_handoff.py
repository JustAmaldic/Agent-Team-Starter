import unittest

from ats_modules import review_handoff


class ReviewHandoffTests(unittest.TestCase):
    def test_relevant_suggestion_reaches_later_report_part(self):
        previous = [{"subtask": 1, "findings": [
            {"id": "F2", "severity": "SUGGESTION", "file": "ats.py",
             "summary": "run_report must filter run_finished without assigned event"},
            {"id": "F3", "severity": "SUGGESTION", "file": "unused.py",
             "summary": "consider unrelated styling in menus"},
        ]}]
        selected = review_handoff.relevant_findings(
            previous, "Build ats_modules/run_report.py and integrate reporting",
            ["ats_modules/run_report.py", "tests/test_run_report.py"])
        self.assertEqual([x["summary"] for x in selected],
                         ["run_report must filter run_finished without assigned event"])
        note = review_handoff.handoff_note(selected)
        self.assertIn("run_finished", note)
        self.assertIn("verify against current code", note)

    def test_major_issues_remain_visible_and_notes_are_bounded(self):
        finding = {"id": "F1", "severity": "MAJOR", "file": "core.py",
                   "summary": "critical invariant in prior implementation"}
        previous = [{"subtask": 1, "findings": [finding] * 20}]
        selected = review_handoff.relevant_findings(previous, "Build report", ["report.py"])
        self.assertEqual(len(selected), 1)
        self.assertLess(len(review_handoff.handoff_note(selected)), 1900)

    def test_does_not_copy_review_output_or_unknown_findings(self):
        previous = [{"subtask": 1, "review": "TOKEN-SECRET",
                     "findings": [{"severity": "SUGGESTION", "summary": "Check run_report contract"},
                                  {"severity": "unknown", "summary": "Harmful secret"}]}]
        note = review_handoff.handoff_note(
            review_handoff.relevant_findings(previous, "Build run_report.py", ["run_report.py"]))
        self.assertNotIn("TOKEN-SECRET", note)
        self.assertNotIn("Harmful secret", note)


if __name__ == "__main__":
    unittest.main()
