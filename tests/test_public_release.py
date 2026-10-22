import pathlib
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats
from scripts.export_public import ROOT, export


class PrivateHistoryTests(unittest.TestCase):
    def test_state_dir_is_not_under_repository(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict('os.environ', {'ATS_STATE_DIR': root}):
                path = ats.state_directory()
                self.assertTrue(path.is_relative_to(pathlib.Path(root)))
                self.assertNotEqual(path, ats.ATS / 'data')

    def test_record_stays_local(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(ats, 'DATA', pathlib.Path(root)):
                event = ats.record('test_event', 'private test content', 'example-task')
                files = list(pathlib.Path(root).rglob('*.json'))
                self.assertEqual(len(files), 1)
                self.assertIn('private test content', files[0].read_text())
                self.assertEqual(event['task_id'], 'example-task')

    def test_sync_out_never_pushes_or_commits(self):
        with patch.object(ats, 'git', side_effect=AssertionError('Git must not run')):
            self.assertIsNone(ats.sync_out())

    def test_clean_export_has_no_private_data_or_git_history(self):
        with tempfile.TemporaryDirectory() as root:
            destination = pathlib.Path(root) / 'public'
            export(destination)
            self.assertTrue((destination / 'ats.py').exists())
            self.assertTrue((destination / 'README.md').exists())
            self.assertFalse((destination / 'data').exists())
            self.assertFalse((destination / '.git').exists())
            self.assertFalse((destination / 'PUBLIC_README.md').exists())
            self.assertFalse((destination / 'PUBLICATION.md').exists())
            self.assertNotIn('data/ARC/events', str(list(destination.rglob('*'))))
            self.assertIn('history-free', (ROOT / 'scripts/export_public.py').read_text())

    def test_export_refuses_existing_folder(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RuntimeError, 'already exists'):
                export(pathlib.Path(root))


if __name__ == '__main__':
    unittest.main()
