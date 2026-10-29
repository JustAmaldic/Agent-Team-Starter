import json
import tempfile
import unittest
from pathlib import Path

import ats
from ats_modules.dag import as_waves, execution_batches, ready_tasks, validate_dag


def example():
    return {"plan_version": 2, "tasks": [
        {"id": "contract", "title": "Define common event contracts", "owned_paths": ["contracts/"], "depends_on": []},
        {"id": "scorer", "title": "Build independent phishing classifier", "owned_paths": ["phish.py"], "depends_on": ["contract"]},
        {"id": "audit", "title": "Build separate newsletter inventory", "owned_paths": ["newsletter.py"], "depends_on": ["contract"]},
        {"id": "state", "title": "Implement per-user persisted state", "owned_paths": ["store.py"], "depends_on": ["contract"]},
        {"id": "finish", "title": "Integrate modules with regression tests", "owned_paths": ["integration.py"], "depends_on": ["scorer", "audit", "state"]},
    ]}


class DAGFoundationTests(unittest.TestCase):
    def test_avoids_legacy_unnecessary_barriers(self):
        graph = example()
        self.assertEqual([n["id"] for n in ready_tasks(graph, {"contract"})], ["scorer", "audit", "state"])
        self.assertEqual([[n["id"] for n in batch] for batch in execution_batches(graph)],
                         [["contract"], ["scorer", "audit"], ["state"], ["finish"]])

    def test_conflicting_paths_are_serialized(self):
        graph = example()
        graph["tasks"][2]["owned_paths"] = ["phish.py"]
        self.assertEqual([len(b) for b in execution_batches(graph)], [1, 2, 1, 1])
        self.assertNotEqual(execution_batches(graph)[1][0]["id"], execution_batches(graph)[1][1]["id"])

    def test_rejects_cycles_and_missing_dependencies(self):
        graph = example()
        graph["tasks"][0]["depends_on"] = ["finish"]
        with self.assertRaisesRegex(ValueError, "Cyclic"):
            validate_dag(graph)
        graph = example()
        graph["tasks"][2]["depends_on"] = ["absent"]
        with self.assertRaisesRegex(ValueError, "Unknown"):
            validate_dag(graph)

    def test_auto_planner_accepts_graph_marker(self):
        output = "ATS_DAG_JSON: " + json.dumps(example())
        self.assertEqual(ats.subtask_plan(output), as_waves(example()))

    def test_auto_planner_rejects_unsafe_graph(self):
        graph = example()
        graph["tasks"][0]["depends_on"] = ["finish"]
        with self.assertRaisesRegex(RuntimeError, "Invalid task DAG"):
            ats.subtask_plan("ATS_DAG_JSON: " + json.dumps(graph))

    def test_legacy_plan_unchanged_and_versioned_plan_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            task = "Improve ARC safety"
            path.write_text(json.dumps({"task": task, **example()}))
            waves = ats.provided_task_plan(path, task)
            self.assertEqual([len(w) if isinstance(w, list) else 1 for w in waves], [1, 2, 1, 1])
            self.assertEqual(waves, as_waves(example()))
            path.write_text(json.dumps({"task": task, "waves": ["Build contract", "Run tests"]}))
            self.assertEqual(ats.provided_task_plan(path, task), ["Build contract", "Run tests"])


if __name__ == "__main__":
    unittest.main()
