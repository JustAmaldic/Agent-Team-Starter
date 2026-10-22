import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats


class LiveOutputTests(unittest.TestCase):
    def test_stream_captures_both_pipes(self):
        with tempfile.TemporaryDirectory() as folder:
            command = [sys.executable, '-u', '-c',
                       "import sys; print('hello', flush=True); print('warning', file=sys.stderr, flush=True)"]
            result = ats.stream_command(command, folder, quiet=True)
            self.assertEqual(result.returncode, 0)
            self.assertIn('hello', result.stdout)
            self.assertIn('warning', result.stderr)

    def test_nonzero_exit_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            result = ats.stream_command([sys.executable, '-c', 'raise SystemExit(7)'], folder, quiet=True)
            self.assertEqual(result.returncode, 7)

    def test_quiet_disables_progress(self):
        with patch.object(ats, 'invoke', return_value='ok') as invoke, \
             patch.object(ats, 'codex_preflight'):
            agent, result = ats.invoke_with_fallback('codex', 'test', '.', set(), 'id', 'implementation', quiet=True)
            self.assertEqual((agent, result), ('codex', 'ok'))
            self.assertTrue(invoke.call_args.kwargs['quiet'])


if __name__ == '__main__':
    unittest.main()
