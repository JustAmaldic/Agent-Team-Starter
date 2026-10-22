import json
import pathlib
import tempfile
import unittest
import uuid

class EventContractTest(unittest.TestCase):
    def test_policy_mentions_required_feedback(self):
        policy=(pathlib.Path(__file__).resolve().parents[1]/'POLICY.md').read_text()
        for text in ('BLOCKER', 'MAJOR', 'MINOR', 'SUGGESTION', 'not applicable', 'deferred'):
            self.assertIn(text,policy)
    def test_example_config_is_json(self):
        root=pathlib.Path(__file__).resolve().parents[1]
        self.assertIn('routing',json.loads((root/'config.example.json').read_text()))

if __name__=='__main__':unittest.main()
