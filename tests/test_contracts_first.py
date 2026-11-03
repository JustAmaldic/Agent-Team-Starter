"""Shared-interface contracts must be reviewed before independent implementations."""
import copy
import unittest

from ats_modules import dag


def contract_plan():
    return {"plan_version": 2, "tasks": [
        {"id": "contract", "title": "Define shared API and type contracts",
         "owned_paths": ["contracts/api.py"], "depends_on": [],
         "contracts_provided": ["user_api"]},
        {"id": "backend", "title": "Implement backend against the stable API",
         "owned_paths": ["src/server.py"], "depends_on": ["contract"],
         "contracts_required": ["user_api"]},
        {"id": "frontend", "title": "Implement frontend against stable API",
         "owned_paths": ["src/client.js"], "depends_on": ["contract"],
         "contracts_required": ["user_api"]},
    ]}


class ContractFirstTests(unittest.TestCase):
    def test_contract_node_precedes_consumers(self):
        plan = dag.validate_dag(contract_plan())
        self.assertEqual(plan["tasks"][0]["contracts_provided"], ["user_api"])
        self.assertEqual([[node["id"] for node in batch] for batch in dag.execution_batches(plan)],
                         [["contract"], ["backend", "frontend"]])

    def test_indirect_dependency_is_allowed(self):
        plan = contract_plan()
        plan["tasks"].append({"id": "integration", "title": "Complete end to end integration tests",
                              "owned_paths": ["tests/test_all.py"], "depends_on": ["backend", "frontend"],
                              "contracts_required": ["user_api"]})
        self.assertEqual(len(dag.validate_dag(plan)["tasks"]), 4)

    def test_missing_contract_provider_is_rejected(self):
        plan = contract_plan()
        plan["tasks"][0]["contracts_provided"] = []
        with self.assertRaisesRegex(ValueError, "Missing contract provider"):
            dag.validate_dag(plan)

    def test_contract_cannot_be_consumed_in_parallel_with_provider(self):
        plan = contract_plan()
        plan["tasks"][1]["depends_on"] = []
        with self.assertRaisesRegex(ValueError, "Contract must be reviewed"):
            dag.validate_dag(plan)

    def test_duplicate_providers_rejected(self):
        plan = contract_plan()
        plan["tasks"][1]["contracts_provided"] = ["user_api"]
        with self.assertRaisesRegex(ValueError, "one owner"):
            dag.validate_dag(plan)

    def test_legacy_dag_does_not_gain_implicit_contract_keys(self):
        plan = contract_plan()
        for node in plan["tasks"]:
            node.pop("contracts_provided", None)
            node.pop("contracts_required", None)
        self.assertEqual(dag.validate_dag(plan), plan)


if __name__ == "__main__":
    unittest.main()
