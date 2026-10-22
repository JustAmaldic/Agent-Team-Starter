"""Support for one central ATS installation driven by a host (ATS_APP, ATS_EVENTS_FILE, ATS_TASK_ID)."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ats

GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't',
           'GIT_COMMITTER_EMAIL': 't@t'}


def read_lines(path):
    return [json.loads(line) for line in pathlib.Path(path).read_text(encoding='utf-8').splitlines() if line]


class AppDirectoryTests(unittest.TestCase):
    def run_python(self, code, env_extra, cwd):
        env = dict(os.environ, **env_extra)
        env.pop('ATS_APP', None) if 'ATS_APP' not in env_extra else None
        return subprocess.run([sys.executable, '-c', code], cwd=cwd, env=env, text=True,
                              capture_output=True, check=True).stdout.strip()

    def test_ats_app_overrides_cwd(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as elsewhere:
            code = f'import sys; sys.path.insert(0, {str(ROOT)!r}); import ats; print(ats.APP)'
            out = self.run_python(code, {'ATS_APP': project}, elsewhere)
            self.assertEqual(pathlib.Path(out), pathlib.Path(project).resolve())

    def test_without_ats_app_cwd_is_used(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            code = f'import sys; sys.path.insert(0, {str(ROOT)!r}); import ats; print(ats.APP)'
            out = self.run_python(code, {}, elsewhere)
            self.assertEqual(pathlib.Path(out).resolve(), pathlib.Path(elsewhere).resolve())


class EventStreamTests(unittest.TestCase):
    def test_record_is_mirrored_to_events_file(self):
        with tempfile.TemporaryDirectory() as temp:
            stream = pathlib.Path(temp) / 'events.jsonl'
            with patch.dict(os.environ, {'ATS_EVENTS_FILE': str(stream)}), \
                 patch.object(ats, 'DATA', pathlib.Path(temp) / 'data'), \
                 patch.object(ats, 'project', return_value={'project': 'P'}):
                ats.record('assigned', 'task · x', 'tid', implementer='codex')
                ats.phase('review', 'tid', cycle=0)
            lines = read_lines(stream)
        self.assertEqual([l['event_type'] for l in lines], ['assigned', 'phase'])
        self.assertEqual(lines[0]['summary'], 'task · x')
        self.assertEqual(lines[1]['stage'], 'review')

    def test_no_variable_no_file_and_unwritable_path_is_ignored(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('ATS_EVENTS_FILE', None)
            ats.emit({'x': 1})   # no error
        with patch.dict(os.environ, {'ATS_EVENTS_FILE': '/nonexistent-dir/x/events.jsonl'}):
            ats.emit({'x': 1})   # no error

    def test_preset_task_id_validation(self):
        good = str(uuid.uuid4())
        with patch.dict(os.environ, {'ATS_TASK_ID': good}):
            self.assertEqual(ats.preset_task_id(), good)
        with patch.dict(os.environ, {'ATS_TASK_ID': 'not-a-uuid'}):
            with self.assertRaisesRegex(RuntimeError, 'must be a UUID'):
                ats.preset_task_id()
        with patch.dict(os.environ, {'ATS_TASK_ID': ''}):
            self.assertIsNone(ats.preset_task_id())


class SelfUpdateTests(unittest.TestCase):
    def test_no_self_update_skips_sync_in(self):
        with patch.object(sys, 'argv', ['ats.py', 'doctor']), \
             patch.dict(os.environ, {'ATS_NO_SELF_UPDATE': '1'}), \
             patch.object(ats, 'sync_in', side_effect=AssertionError('must not update')), \
             patch('builtins.print'):
            self.assertEqual(ats.main(), 0)

    def test_self_update_runs_by_default(self):
        with patch.object(sys, 'argv', ['ats.py', 'doctor']), \
             patch.dict(os.environ, {'ATS_NO_SELF_UPDATE': ''}), \
             patch.object(ats, 'sync_in') as sync, patch('builtins.print'):
            ats.main()
        sync.assert_called_once_with()


class TaskFlowStreamTests(unittest.TestCase):
    """A full task in-process with fake agents; the stream must show every stage in order."""

    def test_stages_are_streamed_with_preset_task_id(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = pathlib.Path(temp)
            app = temp / 'app'
            app.mkdir()
            (app / 'app.py').write_text('x = 1\n')
            (app / '.ats-project.json').write_text(json.dumps(
                {'project': 'App', 'test_commands': [[sys.executable, '-c', 'pass']], 'max_revisions': 1}))
            with patch.dict(os.environ, GIT_ENV):
                subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=app, check=True)
                subprocess.run(['git', 'add', '-A'], cwd=app, check=True)
                subprocess.run(['git', 'commit', '-q', '-m', 'init'], cwd=app, check=True)

            def fake_invoke(agent, prompt, cwd, quiet=False):
                if 'Review ONLY' in prompt:
                    return 'fine\nATS_FINDINGS_JSON: []\nVERDICT: APPROVE'
                (pathlib.Path(cwd) / 'app.py').write_text('x = 2\n')
                return 'done'

            stream = temp / 'events.jsonl'
            task_id = str(uuid.uuid4())
            env = dict(GIT_ENV, ATS_EVENTS_FILE=str(stream), ATS_TASK_ID=task_id)
            with patch.dict(os.environ, env), \
                 patch.object(ats, 'APP', app), \
                 patch.object(ats, 'DATA', temp / 'data'), \
                 patch.object(ats, 'PRIVATE_DATA_REPO', None), \
                 patch.object(ats, 'state_directory', return_value=temp / 'state' / 'data'), \
                 patch.object(ats, 'executable', return_value='/bin/true'), \
                 patch.object(ats, 'codex_preflight'), \
                 patch.object(ats, 'invoke', side_effect=fake_invoke), \
                 patch('builtins.print'):
                ats.task_flow('Change x', 'codex', push=False, quiet=True)
            lines = read_lines(stream)

        self.assertTrue(all(line['task_id'] == task_id for line in lines))
        sequence = [line['stage'] if line['event_type'] == 'phase' else line['event_type'] for line in lines]
        self.assertEqual(sequence, ['assigned', 'implementation', 'implementation', 'checks', 'review',
                                    'review_result', 'approved', 'finished'])
        finished = lines[-1]
        self.assertTrue(finished['approved'])
        self.assertIn('app-ats-' + task_id[:8], finished['worktree'])


if __name__ == '__main__':
    unittest.main()
