"""Regressions for the knowledge loop and event encoding (see PR fixing both)."""
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats


class LearnFileFormatTests(unittest.TestCase):
    def test_learn_writes_valid_json_with_real_newline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            with patch.object(ats, 'PRIVATE_DATA_REPO', root), \
                 patch.object(ats, 'project', return_value={'project': 'ARC'}):
                ats.learn('lesson', 'Check flags', 'Validate CLI flags', 'task-1')
                path = next((root / 'knowledge/projects/ARC/lessons').glob('*.json'))
                raw = path.read_text(encoding='utf-8')
                self.assertTrue(raw.endswith('}\n'))
                self.assertNotIn('\\n', raw[-4:])
                saved = json.loads(raw)
                self.assertEqual(saved['created_by'], 'human')
                self.assertEqual(saved['confidence'], 'medium')

    def test_learn_rejects_unknown_confidence(self):
        with self.assertRaises(RuntimeError):
            ats.learn('lesson', 't', 'o', 'e', confidence='certain')

    def test_learned_then_approved_item_reaches_agent_context(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            with patch.object(ats, 'PRIVATE_DATA_REPO', root), \
                 patch.object(ats, 'project', return_value={'project': 'ARC'}):
                ats.learn('rule', 'Review OAuth', 'Require auth regression tests', 'review-9')
                self.assertEqual(ats.knowledge_context(), '')
                path = next((root / 'knowledge/projects/ARC/rules').glob('*.json'))
                item = json.loads(path.read_text(encoding='utf-8'))
                item['status'] = 'approved'
                path.write_text(json.dumps(item), encoding='utf-8')
                context = ats.knowledge_context()
        self.assertIn('Require auth regression tests', context)
        self.assertTrue(context.startswith('\nRelevant vetted'))
        self.assertNotIn('\\n', context)


class CaptureProposalTests(unittest.TestCase):
    def capture(self, text):
        with patch.object(ats, 'learn', side_effect=lambda *a, **k: (a, k)) as learn:
            result = ats.capture_learning_proposals(text, 'task-7')
        return learn, result

    def test_echoed_prompt_template_is_ignored_and_last_line_wins(self):
        echoed = ('user\nRead POLICY.md. Task: x. At the end include ONE LINE ATS_LEARNINGS_JSON: '
                  '[{"kind":"finding","title":"<title>","observation":"<observation>",'
                  '"evidence":"<evidence>"}] (replace the placeholders, or use []).\n')
        answer = ('codex\nDone.\nATS_LEARNINGS_JSON: [{"kind":"finding","title":"Missing retry",'
                  '"observation":"No retry on HTTP 503","evidence":"tests/test_api.py:20"}]\n')
        learn, result = self.capture(echoed + answer)
        self.assertEqual(learn.call_count, 1)
        self.assertEqual(learn.call_args.args[1], 'Missing retry')
        self.assertEqual(learn.call_args.kwargs['created_by'], 'agent')

    def test_placeholder_values_are_rejected(self):
        text = ('ATS_LEARNINGS_JSON: [{"kind":"finding","title":"...","observation":"...",'
                '"evidence":"test/review/task reference"}]')
        learn, _ = self.capture(text)
        learn.assert_not_called()

    def test_empty_list_and_garbage_are_ignored(self):
        for text in ('ATS_LEARNINGS_JSON: []', 'ATS_LEARNINGS_JSON: [not json]', 'nothing here'):
            learn, _ = self.capture(text)
            learn.assert_not_called()

    def test_windows_line_endings(self):
        text = ('ATS_LEARNINGS_JSON: [{"kind":"lesson","title":"T","observation":"O",'
                '"evidence":"E"}]\r\nmore')
        learn, _ = self.capture(text)
        self.assertEqual(learn.call_count, 1)


class EventEncodingTests(unittest.TestCase):
    def test_record_bytes_are_utf8(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(ats, 'DATA', pathlib.Path(temp)), \
                 patch.object(ats, 'project', return_value={'project': 'ARC'}):
                ats.record('error', 'limit · reset')
                raw = next(pathlib.Path(temp).rglob('*.json')).read_bytes()
        self.assertIn('·'.encode('utf-8'), raw)

    def test_read_json_accepts_legacy_cp1252_events(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / 'legacy.json'
            path.write_bytes('{"summary": "limit · resets"}'.encode('cp1252'))
            self.assertEqual(ats.read_json(path)['summary'], 'limit · resets')

    def test_select_survives_legacy_event_files(self):
        with tempfile.TemporaryDirectory() as temp:
            events = pathlib.Path(temp) / 'ARC' / 'events'
            events.mkdir(parents=True)
            (events / 'a.json').write_bytes(json.dumps(
                {'event_type': 'review_result', 'implementer': 'claude', 'approved': True,
                 'summary': 'x'}).replace('x', '·').encode('cp1252'))
            with patch.object(ats, 'DATA', pathlib.Path(temp)), \
                 patch.object(ats, 'project', return_value={'project': 'ARC'}):
                self.assertIn(ats.select('neutral'), ('codex', 'claude'))


if __name__ == '__main__':
    unittest.main()
