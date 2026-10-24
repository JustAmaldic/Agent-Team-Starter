"""Learnings from the review process: distilled automatically, approved unless drastic."""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats


def line(*items):
    return 'ATS_LEARNINGS_JSON: ' + json.dumps(list(items))


class AutoLearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.patches = [patch.object(ats, 'PRIVATE_DATA_REPO', self.root),
                        patch.object(ats, 'project', return_value={'project': 'ATS-App'})]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    def saved(self):
        return [json.loads(p.read_text()) for p in (self.root / 'knowledge').rglob('*.json')]

    def test_risk_check(self):
        for text in ('Use git reset --hard before retrying', 'Force push the branch after a rebase',
                     'Delete the old worktree folders automatically', 'Skip the review for docs-only changes',
                     'Merge directly into main when tests pass', 'Store the API key in the config file',
                     'Verwijder oude bestanden in de map data', 'Goedkeuren zonder tests is prima bij kleine fixes'):
            self.assertIsNotNone(ats.learning_risk(text), text)
        for text in ('Background retries back off and log once per outage',
                     'Helper kills the ATS window which HomeBase does not restart',
                     'Write text files explicitly as UTF-8', 'A rollback must never stop halfway'):
            self.assertIsNone(ats.learning_risk(text), text)

    def test_low_risk_is_approved_risky_waits_shared_goes_to_shared(self):
        output = line(
            {'kind': 'lesson', 'title': 'Back off retries', 'observation': 'Use 1, 2, 4 minutes',
             'evidence': 'x.py:1', 'scope': 'shared', 'risk': 'low'},
            {'kind': 'rule', 'title': 'Clean up', 'observation': 'Delete stale worktree folders after each run',
             'evidence': 'y.py:2', 'risk': 'low'},
            {'kind': 'lesson', 'title': 'Agent says risky', 'observation': 'Rewrite the release flow',
             'evidence': 'z.py:3', 'risk': 'high'},
            {'kind': 'lesson', 'title': 'back off  RETRIES', 'observation': 'same title again',
             'evidence': 'x.py:9'})
        saved = ats.capture_learning_proposals(output, 'task-1')
        status = {i['title']: (i['status'], i.get('needs_human'), i['project']) for i in saved}
        self.assertEqual(status['Back off retries'], ('approved', None, 'shared'))
        self.assertEqual(status['Clean up'][0:2], ('proposed', 'deletes data'))
        self.assertEqual(status['Agent says risky'][0:2], ('proposed', 'marked high risk by the agent'))
        self.assertEqual(len(saved), 3)   # the repeated title is skipped
        self.assertTrue((self.root / 'knowledge/shared/lessons').is_dir())
        context = ats.knowledge_context()
        self.assertIn('Back off retries', context)
        self.assertNotIn('Delete stale worktree', context)

    def test_human_cli_learn_stays_a_proposal(self):
        item = ats.learn('rule', 'Always X', 'Because Y', 'review #1')
        self.assertEqual(item['status'], 'proposed')

    def test_distill_learnings_from_findings_is_read_only(self):
        folder = self.root / 'work'
        folder.mkdir()
        subprocess.run(['git', 'init', '-q'], cwd=folder, check=True)
        (folder / 'app.py').write_text('x = 1\n')
        history = [{'cycle': 0, 'reviewer': 'claude',
                    'findings': [{'id': 'F1', 'severity': 'MAJOR', 'file': 'app.py', 'line': '1',
                                  'summary': 'No lock around updates'}],
                    'responses': [{'id': 'F1', 'status': 'fixed', 'note': 'lock added'}]}]
        prompts = []

        def invoke(agent, prompt, cwd, unavailable, task_id, purpose, quiet=False, fallback=True):
            prompts.append((agent, prompt))
            (folder / 'app.py').write_text('tampered\n')   # must be reverted
            return agent, prompt + '\n' + line({'kind': 'rule', 'title': 'One update at a time',
                                                'observation': 'Hold an exclusive lock for the whole update',
                                                'evidence': 'F1 app.py:1', 'risk': 'low'})

        with patch.object(ats, 'invoke_with_fallback', side_effect=invoke), \
             patch.object(ats, 'record'), patch.object(ats, 'phase'):
            saved = ats.distill_learnings('task', 'tid', folder, history, 'claude', ats.UnavailableAgents(), quiet=True)
        self.assertEqual([(i['title'], i['status']) for i in saved], [('One update at a time', 'approved')])
        self.assertIn('No lock around updates', prompts[0][1])
        self.assertEqual((folder / 'app.py').read_text(), 'x = 1\n')

    def test_no_findings_means_no_agent_call(self):
        with patch.object(ats, 'invoke_with_fallback') as invoke:
            self.assertEqual(ats.distill_learnings('t', 'tid', self.root, [{'cycle': 0, 'findings': []}],
                                                   'claude', ats.UnavailableAgents()), [])
        invoke.assert_not_called()

    def test_failure_never_breaks_the_task(self):
        folder = self.root / 'w'
        folder.mkdir()
        subprocess.run(['git', 'init', '-q'], cwd=folder, check=True)
        history = [{'cycle': 0, 'findings': [{'id': 'F', 'severity': 'MINOR', 'summary': 's'}]}]
        with patch.object(ats, 'invoke_with_fallback', side_effect=ats.AgentUsageExhausted('claude')), \
             patch.object(ats, 'phase'):
            self.assertEqual(ats.distill_learnings('t', 'tid', folder, history, 'claude', ats.UnavailableAgents()), [])


if __name__ == '__main__':
    unittest.main()
