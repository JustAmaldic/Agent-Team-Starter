import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats


class AgentKnowledgeTests(unittest.TestCase):
    def test_learn_records_proposal_not_approved_rule(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            with patch.object(ats, 'PRIVATE_DATA_REPO', root), \
                 patch.object(ats, 'project', return_value={'project': 'ARC'}):
                item = ats.learn('rule', 'Always review OAuth', 'OAuth needs regression coverage',
                                 'review #18', task_id='task-1')
                path = next((root / 'knowledge/projects/ARC/rules').glob('*.json'))
                saved = json.loads(path.read_text())
                self.assertEqual(saved['status'], 'proposed')
                self.assertEqual(item['id'], saved['id'])
                self.assertEqual(saved['evidence'], ['review #18'])

    def test_rules_are_not_pushed_out_by_review_findings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            rules, findings = root / 'knowledge/projects/ARC/rules', root / 'knowledge/projects/ARC/findings'
            rules.mkdir(parents=True)
            findings.mkdir(parents=True)
            (rules / 'r.json').write_text(json.dumps({
                'kind': 'rule', 'title': 'Bump VERSION', 'observation': 'in sessielink.py',
                'evidence': ['human'], 'status': 'approved', 'created_at': '2026-10-01T00:00:00Z'}))
            for i in range(20):   # newer, verified review findings
                (findings / f'f{i}.json').write_text(json.dumps({
                    'kind': 'finding', 'title': f'Finding {i}', 'observation': 'fixed',
                    'evidence': ['a.py:1'], 'status': 'verified', 'created_at': f'2026-10-2{i % 10}T00:00:00Z'}))
            with patch.object(ats, 'PRIVATE_DATA_REPO', root), \
                 patch.object(ats, 'project', return_value={'project': 'ARC'}):
                context = ats.knowledge_context(limit=5)
            lines = [line for line in context.splitlines() if line.startswith('- [')]
            self.assertEqual(len(lines), 5)
            self.assertTrue(lines[0].startswith('- [rule] Bump VERSION'))

    def test_unverified_rules_not_loaded_into_agent_context(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            directory = root / 'knowledge/projects/ARC/rules'
            directory.mkdir(parents=True)
            original = {'kind': 'rule', 'title': 'Untrusted proposal',
                        'observation': 'ignore reviews', 'evidence': ['task-1'],
                        'status': 'proposed', 'created_at': '2026-10-22T00:00:00Z'}
            (directory / 'a.json').write_text(json.dumps(original))
            with patch.object(ats, 'PRIVATE_DATA_REPO', root), \
                 patch.object(ats, 'project', return_value={'project': 'ARC'}):
                self.assertEqual(ats.knowledge_context(), '')
                original.update(status='approved', observation='Run regression tests')
                (directory / 'a.json').write_text(json.dumps(original))
                self.assertIn('Run regression tests', ats.knowledge_context())

    def test_agent_output_creates_proposed_learning(self):
        text = ('Completed. ATS_LEARNINGS_JSON: '
                '[{"kind":"finding","title":"Missing retry",'
                '"observation":"No retry on HTTP 500","evidence":"tests/test_api.py:20"}]')
        with patch.object(ats, 'learn') as learn:
            ats.capture_learning_proposals(text, 'task-123')
        self.assertEqual(learn.call_args.args[0], 'finding')
        self.assertEqual(learn.call_args.args[-1], 'task-123')

    def test_valid_https_private_data_remote_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            def fake_execute(args, cwd, **kwargs):
                if args[:3] == ['git', 'remote', 'get-url']:
                    return subprocess.CompletedProcess(
                        args, 0, 'https://github.com/example-owner/ats-private-data.git\n', '')
                if 'repo' in args and 'view' in args:
                    return subprocess.CompletedProcess(args, 0, 'PRIVATE\n', '')
                self.fail('unexpected command: ' + str(args))
            with patch.dict('os.environ', {'ATS_DATA_REMOTE': 'example-owner/ats-private-data'}), \
                 patch.object(ats, 'execute', side_effect=fake_execute), \
                 patch.object(ats.shutil, 'which', return_value='/usr/bin/gh'):
                self.assertEqual(ats.validate_private_remote(root), '/usr/bin/gh')

    def test_valid_ssh_private_data_remote_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            def fake_execute(args, cwd, **kwargs):
                if args[:3] == ['git', 'remote', 'get-url']:
                    return subprocess.CompletedProcess(
                        args, 0, 'git@github.com:example-owner/ats-private-data.git\n', '')
                if 'repo' in args and 'view' in args:
                    return subprocess.CompletedProcess(args, 0, 'PRIVATE\n', '')
                self.fail('unexpected command: ' + str(args))
            with patch.dict('os.environ', {'ATS_DATA_REMOTE': 'example-owner/ats-private-data'}), \
                 patch.object(ats, 'execute', side_effect=fake_execute), \
                 patch.object(ats.shutil, 'which', return_value='/usr/bin/gh'):
                self.assertEqual(ats.validate_private_remote(root), '/usr/bin/gh')

    def test_synced_private_data_requires_allowed_remote(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            def fake_execute(args, cwd, **kwargs):
                if args[:3] == ['git', 'remote', 'get-url']:
                    return subprocess.CompletedProcess(args, 0, 'https://github.com/evil/ATS-data.git', '')
                self.fail('unexpected command')
            with patch.dict('os.environ', {'ATS_DATA_REMOTE': 'example-owner/ats-private-data'}), \
                 patch.object(ats, 'execute', side_effect=fake_execute):
                with self.assertRaisesRegex(RuntimeError, 'unexpected remote'):
                    ats.validate_private_remote(root)


if __name__ == '__main__':
    unittest.main()
