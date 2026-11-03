"""The baseline script: fixture, summary per task and comparison (no real agents)."""
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import benchmark


class BenchmarkTests(unittest.TestCase):
    def test_fixture_is_a_committed_repo_with_passing_tests(self):
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            app = benchmark.build_fixture(pathlib.Path(temp))
            self.assertEqual(subprocess.run(['git', 'status', '--porcelain'], cwd=app, capture_output=True,
                                            text=True).stdout, '')
            result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'], cwd=app,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_summary_and_compare(self):
        events = [
            {'event_type': 'agent_usage', 'purpose': 'implementation', 'input_tokens': 100, 'output_tokens': 50,
             'cache_read_tokens': 1000, 'billable_tokens': 250},
            {'event_type': 'agent_usage', 'purpose': 'review', 'input_tokens': 10, 'billable_tokens': 10,
             'session_resumed': True},
            {'event_type': 'review_result', 'approved': True},
            {'event_type': 'phase', 'stage': 'finished', 'approved': True},
        ]
        summary = benchmark.summarize(events, 12.5)
        self.assertTrue(summary['approved'])
        self.assertEqual(summary['billable_total'], 260)
        self.assertEqual(summary['tokens']['implementation']['cache_read_tokens'], 1000)
        self.assertEqual(summary['sessions_resumed'], 1)
        before = {'tasks': {'bugfix': summary}}
        after = {'tasks': {'bugfix': dict(summary, billable_total=130)}}
        self.assertEqual(benchmark.compare(before, after), ['bugfix: tokens 260 -> 130 (-50%), quality same'])
        self.assertNotIn('prompt', json.dumps(summary))


if __name__ == '__main__':
    unittest.main()
