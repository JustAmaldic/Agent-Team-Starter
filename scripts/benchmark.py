#!/usr/bin/env python3
"""Baseline for ATS: fixed tasks on a small fixture repository, measured with real agent CLIs.

Use it as the agent contract test and as the before/after measurement for changes to prompts,
routing, review or models. It spends real tokens and usage limit, so run it on purpose (not in CI
and not for every pull request):

    python3 scripts/benchmark.py run --out ~/ats-benchmarks/2026-11-03.json
    python3 scripts/benchmark.py compare before.json after.json

Per task it records: approved or not, review rounds, gate repairs, wall time and the tokens per
phase from the `agent_usage` events (new input, output, cache read, cache write). It never stores
prompts or answers. `run --dry-run` only builds the fixture and prints the tasks.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from collections import defaultdict
from pathlib import Path

ATS = Path(__file__).resolve().parents[1]

FIXTURE = {
    'README.md': '# Bench app\n\nA tiny shop backend used to measure ATS.\n',
    'shop/__init__.py': '',
    'shop/prices.py': ('def total(items):\n    """Sum of price * quantity."""\n'
                       '    return sum(i["price"] * i["quantity"] for i in items)\n'),
    'shop/client.py': ('import urllib.request\n\n\ndef fetch(url):\n'
                       '    with urllib.request.urlopen(url) as response:\n        return response.read()\n'),
    'shop/names.py': 'def slug(name):\n    return name.lower().replace(" ", "-")\n',
    'tests/__init__.py': '',
    'tests/test_prices.py': ('import unittest\n\nfrom shop.prices import total\n\n\n'
                             'class PriceTests(unittest.TestCase):\n    def test_total(self):\n'
                             '        self.assertEqual(total([{"price": 2, "quantity": 3}]), 6)\n'),
    '.ats-project.json': json.dumps({'project': 'BenchApp', 'test_commands': ['python3 -m unittest discover -s tests'],
                                     'max_revisions': 2}, indent=2) + '\n',
}

# Fixed tasks with acceptance criteria; keep them stable so results stay comparable.
TASKS = [
    ('bugfix', 'Fix shop/prices.py total() so items without a quantity count as quantity 1. '
               'Accept: a unit test covers the missing quantity.'),
    ('feature', 'Add a discount(percent) helper to shop/prices.py that rounds to cents and rejects '
                'percentages outside 0-100. Accept: unit tests for rounding and both limits.'),
    ('robustness', 'Give shop/client.py fetch() a timeout parameter (default 10 seconds) and raise a clear '
                   'error on HTTP errors. Accept: tests with a fake opener, no network.'),
    ('refactor', 'Refactor shop/names.py slug() to also strip accents and collapse repeated dashes, keeping '
                 'existing behaviour. Accept: tests for accents, spaces and dashes.'),
    ('docs', 'Document every public function of the shop package in README.md with a one-line example each. '
             'Accept: README lists total, fetch and slug.'),
    ('tests', 'Add unit tests for shop/names.py slug() covering empty strings and mixed case. '
              'Accept: tests pass and do not change slug().'),
]


def build_fixture(root: Path) -> Path:
    app = root / 'bench-app'
    for name, text in FIXTURE.items():
        path = app / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    env = dict(os.environ, GIT_AUTHOR_NAME='bench', GIT_AUTHOR_EMAIL='bench@localhost',
               GIT_COMMITTER_NAME='bench', GIT_COMMITTER_EMAIL='bench@localhost')
    for args in (['init', '-q', '-b', 'main'], ['add', '-A'], ['commit', '-q', '-m', 'fixture']):
        subprocess.run(['git', *args], cwd=app, env=env, check=True)
    return app


def read_events(path: Path):
    events = []
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return events
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def summarize(events, seconds=None):
    """One task's measurement from its event stream."""
    tokens = defaultdict(lambda: defaultdict(int))
    for e in events:
        if e.get('event_type') != 'agent_usage':
            continue
        phase = e.get('purpose') or 'unknown'
        for field in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'billable_tokens'):
            tokens[phase][field] += int(e.get(field) or 0)
        tokens[phase]['calls'] += 1
    reviews = [e for e in events if e.get('event_type') == 'review_result']
    finished = next((e for e in reversed(events) if e.get('event_type') == 'phase' and e.get('stage') == 'finished'), {})
    return {
        'approved': bool(finished.get('approved')),
        'review_rounds': len(reviews),
        'gate_repairs': sum(e.get('event_type') == 'gate_repair' for e in events),
        'escalated': any(e.get('event_type') == 'escalated' for e in events),
        'sessions_resumed': sum(bool(e.get('session_resumed')) for e in events if e.get('event_type') == 'agent_usage'),
        'seconds': seconds,
        'tokens': {phase: dict(values) for phase, values in sorted(tokens.items())},
        'billable_total': sum(v['billable_tokens'] for v in tokens.values()),
    }


def run(args):
    with tempfile.TemporaryDirectory(prefix='ats-bench-') as temp:
        app = build_fixture(Path(temp))
        if args.dry_run:
            print(f'Fixture: {app}')
            for name, task in TASKS:
                print(f'- {name}: {task}')
            return 0
        results = {}
        for name, task in TASKS:
            if args.only and name not in args.only:
                continue
            stream = Path(temp) / f'{name}.jsonl'
            env = dict(os.environ, ATS_APP=str(app), ATS_EVENTS_FILE=str(stream), ATS_TASK_ID=str(uuid.uuid4()))
            started = time.monotonic()
            subprocess.run([sys.executable, str(ATS / 'ats.py'), 'task', task, '--quiet'], cwd=app, env=env,
                           check=False)
            results[name] = summarize(read_events(stream), round(time.monotonic() - started, 1))
            print(f"{name}: approved={results[name]['approved']} rounds={results[name]['review_rounds']} "
                  f"billable={results[name]['billable_total']}", flush=True)
        report = {'created': time.strftime('%Y-%m-%dT%H:%M:%S'), 'ats_commit': git_head(), 'tasks': results}
        Path(args.out).expanduser().parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).expanduser().write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        print('Saved', args.out)
    return 0


def git_head():
    result = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=ATS, capture_output=True, text=True)
    return result.stdout.strip() or None


def compare(before, after):
    """Lines that compare two reports task by task: quality first, then tokens."""
    lines = []
    for name in sorted(set(before['tasks']) | set(after['tasks'])):
        a, b = before['tasks'].get(name), after['tasks'].get(name)
        if not a or not b:
            lines.append(f'{name}: only in {"after" if b else "before"}')
            continue
        change = (b['billable_total'] - a['billable_total']) / a['billable_total'] * 100 if a['billable_total'] else 0
        quality = 'same' if (a['approved'], a['review_rounds']) == (b['approved'], b['review_rounds']) else (
            f"approved {a['approved']}->{b['approved']}, rounds {a['review_rounds']}->{b['review_rounds']}")
        lines.append(f"{name}: tokens {a['billable_total']} -> {b['billable_total']} ({change:+.0f}%), quality {quality}")
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('run')
    p.add_argument('--out', default=str(Path.home() / 'ats-benchmarks' / (time.strftime('%Y%m%d-%H%M%S') + '.json')))
    p.add_argument('--only', nargs='*')
    p.add_argument('--dry-run', action='store_true')
    p = sub.add_parser('compare')
    p.add_argument('before')
    p.add_argument('after')
    args = parser.parse_args(argv)
    if args.command == 'run':
        return run(args)
    before, after = (json.loads(Path(x).expanduser().read_text(encoding='utf-8')) for x in (args.before, args.after))
    print('\n'.join(compare(before, after)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
