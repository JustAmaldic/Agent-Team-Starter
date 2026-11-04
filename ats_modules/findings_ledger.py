"""Who fixes which finding: a register of every review finding of a planned task.

Passing earlier findings to the next part as context is not the same as getting them fixed. In one
run a reviewer found a real bug in part 1, part 1 was approved (the finding was MINOR), part 2 said
it could not fix it because the file belonged to part 1, and nobody fixed it.

Every finding gets a stable key (`part1/security/SF1`), the part that owns its file, and a status:

- open: listed by the last review of its part (the part was approved with it still standing);
- fixed / not_applicable / deferred: the implementer's answer, when a later review of the same
  part no longer lists it.

Open and deferred findings (MINOR and up) go to the part that owns the file when that part still
has to run, and otherwise to one fix-up step before the integration review. A review that reports
nothing new never closes them silently: only a fix the reviewer confirms does.
"""
from __future__ import annotations

import json

ATTENTION = ('BLOCKER', 'MAJOR', 'MINOR')
STILL_OPEN = ('open', 'deferred')
MAX_NOTE = 2500


def _owner(path, plan, default):
    from . import dag as dag_planning
    if not path or not isinstance(plan, dict):
        return default
    for node in plan.get('tasks', []):
        if dag_planning.paths_conflict([path], node.get('owned_paths') or []):
            return node['id']
    return default


def _node_id(plan, number):
    if isinstance(plan, dict):
        tasks = plan.get('tasks', [])
        if isinstance(number, int) and 1 <= number <= len(tasks):
            return tasks[number - 1]['id']
    return f'part{number}' if number is not None else 'task'


def build(history, plan=None):
    """The register from the review cycles of a task (subtask, integration and update cycles)."""
    groups = {}
    for cycle in history:
        where = 'integration' if cycle.get('integration') else (
            f"part{cycle['subtask']}" if cycle.get('subtask') is not None else 'task')
        groups.setdefault(where, []).append(cycle)
    entries = {}
    for where, cycles in groups.items():
        number = cycles[0].get('subtask')
        node = _node_id(plan, number) if where.startswith('part') else where
        last = cycles[-1]
        for index, cycle in enumerate(cycles):
            answers = {r.get('id'): r for r in cycle.get('responses') or []}
            for finding in cycle.get('findings') or []:
                kind = 'security' if str(finding.get('id', '')).startswith('S') else 'review'
                key = f"{where}/{kind}/{finding.get('id')}"
                answer = answers.get(finding.get('id')) or {}
                status = 'open' if cycle is last else (answer.get('status') or 'open')
                entries[key] = {
                    'key': key, 'part': node, 'severity': finding.get('severity'),
                    'file': finding.get('file') or '', 'line': finding.get('line') or '',
                    'summary': finding.get('summary') or '', 'status': status,
                    'owner': _owner(finding.get('file'), plan, node),
                    'note': answer.get('note') or ''}
            # A later review of the same part that lists the finding again overwrites it (same key).
    return list(entries.values())


def resolve(entries, checked_ids, checker='integration review'):
    """After a review that was given the open entries: those it lists again stay open, the others
    are confirmed fixed. Entries the review did not get are unchanged."""
    checked_ids = set(checked_ids)
    result = []
    for entry in entries:
        if entry in needs_attention(entries):
            if entry['key'] in checked_ids:
                entry = dict(entry, status='open')
            else:
                entry = dict(entry, status='fixed', note=f'confirmed by the {checker}')
        result.append(entry)
    return result


def needs_attention(entries):
    return [e for e in entries if e['status'] in STILL_OPEN and e['severity'] in ATTENTION]


def for_owner(entries, node_id):
    return [e for e in needs_attention(entries) if e['owner'] == node_id]


def note(entries, header):
    if not entries:
        return ''
    items = [{'key': e['key'], 'severity': e['severity'], 'file': e['file'], 'line': e['line'],
              'summary': e['summary'][:300], 'status': e['status']} for e in entries]
    return header + json.dumps(items, ensure_ascii=False)[:MAX_NOTE] + '\n'


def owner_note(entries):
    return note(entries, 'OPEN FINDINGS IN FILES THIS PART OWNS (from earlier independent reviews; fix '
                         'them in this part, or explain in your summary why not; the reviewer checks each '
                         'and lists any that still stand with the same key as id): ')


def integration_note(entries):
    return note(entries, 'OPEN FINDINGS FROM THE PART REVIEWS (verify each against the combined code; list '
                         'every one that still stands in ATS_FINDINGS_JSON with its key as id and its '
                         'severity; do not drop one without checking it is fixed): ')


def summary_lines(entries):
    """Short lines for the PR description."""
    return [f"- {e['severity']} {e['key']} {e['file']}{(':' + e['line']) if e['line'] else ''}: "
            f"{e['summary'][:160]} ({e['status']})" for e in needs_attention(entries)]
