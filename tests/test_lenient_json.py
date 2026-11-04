"""Agent JSON with an unescaped quote copied from the task is repaired; real errors say where."""
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ats
from ats_modules import lenient_json


def dag_line(title_b):
    return ('ATS_DAG_JSON: {"plan_version":2,"tasks":['
            '{"id":"core","title":"Build the template module with tests","depends_on":[],"owned_paths":["t.py"]},'
            '{"id":"ui","title":"' + title_b + '","depends_on":["core"],"owned_paths":["t.js"]}]}')


class RepairTests(unittest.TestCase):
    def test_inner_quotes_are_escaped(self):
        text = '{"title": "Koppel het "Nieuwe run"-formulier, met een "Start"-knop", "id": "x"}'
        self.assertEqual(lenient_json.loads(text),
                         {'title': 'Koppel het "Nieuwe run"-formulier, met een "Start"-knop', 'id': 'x'})

    def test_quote_before_comma_and_text_stays_inside(self):
        text = '{"title": "Toon "Opslaan", daarna de lijst"}'
        self.assertEqual(lenient_json.loads(text)['title'], 'Toon "Opslaan", daarna de lijst')

    def test_valid_json_is_unchanged(self):
        text = json.dumps({'a': 'say \"hi\"', 'b': [1, 2], 'c': {'d': ''}})
        self.assertEqual(lenient_json.loads(text), json.loads(text))
        self.assertEqual(lenient_json.repair_quotes(text), text)

    def test_other_errors_name_the_place(self):
        with self.assertRaises(ValueError) as caught:
            lenient_json.loads('{"a": [1 2]}')
        self.assertIn('>>>HERE>>>', str(caught.exception))
        self.assertIn('Escape double quotes', str(caught.exception))


class PlanParsingTests(unittest.TestCase):
    def test_dag_with_a_quoted_ui_label_is_accepted(self):
        plan = ats.subtask_plan(dag_line('Koppel sjablonen aan het "Nieuwe run"-formulier met een frontendtest'))
        self.assertEqual(plan['tasks'][1]['title'],
                         'Koppel sjablonen aan het "Nieuwe run"-formulier met een frontendtest')

    def test_broken_dag_tells_the_planner_where(self):
        with self.assertRaises(RuntimeError) as caught:
            ats.subtask_plan('ATS_DAG_JSON: {"plan_version":2,"tasks":[{"id":"a" "title":"x"}]}')
        self.assertIn('>>>HERE>>>', str(caught.exception))

    def test_findings_marker_survives_an_inner_quote(self):
        line = 'ATS_FINDINGS_JSON: [{"id":"F1","severity":"MINOR","summary":"Knop "Start" mist focusstijl"}]'
        self.assertEqual(ats.parse_marked_json(line, 'ATS_FINDINGS_JSON')[0]['summary'],
                         'Knop "Start" mist focusstijl')


if __name__ == '__main__':
    unittest.main()
