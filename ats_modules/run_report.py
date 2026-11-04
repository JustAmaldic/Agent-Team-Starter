"""A short markdown report per run, built only from the stored events of that run.

The report holds counts, durations, agent names and escalation reasons. It never holds prompts, the
task description, review text, finding summaries or other agent output: only fields the events
already store for that purpose. Escalation reasons are fixed ATS labels, because the stored reasons
can contain CLI output, check output or exception text. What older runs did not record, or recorded
only for some events, is shown as unknown, never guessed or counted as zero.

Durations: the total is the wall time of run_finished_v1, so parallel parts are not added twice.
A part's own duration comes from wave_timing when it ran in a parallel wave, otherwise from its
budget event (written when the part starts) to the event that ends it (subtask_completed, or the
escalation, block or error of that part); without both the duration is unknown. A part reused from
a checkpoint (subtask_resumed) was approved in an earlier run: it counts as approved, its agents
come from that event or from subtasks_completed, and its duration here is unknown. Repairing the
merge of a parallel part happens after the wave and is shown separately, from merge_conflict to
subtask_merged or the stored failure event.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from . import usage

SEVERITIES = ('BLOCKER', 'MAJOR', 'MINOR', 'SUGGESTION')
UNKNOWN = 'unknown'
MARKER = '<!-- ats-run-report '
OTHER_REASON = 'other reason (details in the event log)'
RUN_ERROR = 'run error (details in the event log)'
USAGE_LIMIT = 'agent usage limit reached'
# The start of every blocking reason and escalation summary ats.py writes, and the label the report
# shows instead. Only a part number is taken over from the stored text.
REASONS = (
    (r'not reviewed$', 'not reviewed'),
    (r'budget: ', 'budget spent'),
    (r'stuck: ', 'stuck: no progress between rounds'),
    (r'pre-review checks keep failing', 'pre-review checks keep failing after automatic repairs'),
    (r'repair of the pre-review checks impossible', 'repair of the pre-review checks impossible'),
    (r'test command cannot start', 'test command cannot start on this computer'),
    (r'reviewer modified the workspace', 'reviewer modified the workspace'),
    (r'no independent reviewer', 'no independent reviewer available'),
    (r'diff too large', 'diff too large to review completely'),
    (r'reviewer usage exhausted', 'reviewer usage exhausted'),
    (r'review failed$', 'review failed'),
    (r'security review pass failed', 'security review pass failed'),
    (r'security pass: ', 'security pass not approved'),
    (r'revision impossible', 'revision impossible: agent usage limit'),
    (r'verdict: ', 'verdict was not APPROVE'),
    (r'failing checks', 'failing checks'),
    (r'no test_commands configured', 'no test_commands configured'),
    (r'open (?:BLOCKER/MAJOR findings|(?:BLOCKER|MAJOR) )', 'open BLOCKER/MAJOR findings'),
    (r'unanswered (?:BLOCKER|MAJOR) ', 'unanswered BLOCKER/MAJOR findings'),
    (r'deferred BLOCKER ', 'deferred BLOCKER finding'),
    (r'subtask (\d+): ', 'part {} could not be completed'),
    (r'merge of part (\d+): ', 'merge of part {} could not be completed'),
    (r'update with \S+: ', 'update with the base branch could not be completed'),
    (r'Not published: ', 'not published: the update with the base branch needs a human decision'),
    (r'Both agents are required', 'both agents are required for part handoffs'),
    (r'Agent made no changes', 'agent made no changes'),
)


def _time(value):
    try:
        moment = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def _part(event):
    value = event.get('subtask')
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _label(text):
    """The fixed label of a stored reason, or None when ats.py does not write it this way."""
    text = str(text).strip()
    for pattern, label in REASONS:
        match = re.match(pattern, text)
        if match:
            return label.format(*match.groups())
    return None


def _escalation(events, outcome):
    """Labels for the last escalation's blocking reasons, or for the last block or error."""
    if outcome not in ('escalated', 'blocked', 'error'):
        return []
    last = next((e for e in reversed(events) if e.get('event_type') == outcome), None)
    if last is None:
        return []
    if outcome == 'blocked' and 'resets' in last:
        return [USAGE_LIMIT]
    reasons = last.get('blocking_reasons') if isinstance(last.get('blocking_reasons'), list) else []
    reasons = [r for r in reasons or [last.get('summary')] if str(r or '').strip()]
    fallback = RUN_ERROR if outcome == 'error' else OTHER_REASON
    return list(dict.fromkeys(_label(r) or fallback for r in reasons))


def _part_seconds(mine):
    """From the part's start to the event that ended it, or None when either is not stored."""
    start = next((i for i, e in enumerate(mine) if e.get('event_type') == 'budget'), None)
    if start is None:
        return None
    end = None
    for event in mine[start + 1:]:
        kind = event.get('event_type')
        if kind == 'merge_conflict':   # integration work after the part is reported separately
            break
        if kind in ('subtask_completed', 'escalated', 'blocked', 'error'):
            end = event
            break
    first, last = _time(mine[start].get('timestamp')), _time((end or {}).get('timestamp'))
    return round((last - first).total_seconds(), 1) if first and last and last >= first else None


def _integrations(events):
    """Merge repairs have their own interval, outside the parallel build and its barrier wait."""
    result = []
    for index, start in enumerate(events):
        if start.get('event_type') != 'merge_conflict':
            continue
        number = _part(start)
        mine = []
        end = {}
        for event in events[index + 1:]:
            if _part(event) != number:
                continue
            kind = event.get('event_type')
            if kind == 'merge_conflict':
                break
            mine.append(event)
            if kind in ('subtask_merged', 'base_merged', 'escalated', 'blocked', 'error'):
                end = event
                break
        first, last = _time(start.get('timestamp')), _time(end.get('timestamp'))
        seconds = round((last - first).total_seconds(), 1) if first and last and last >= first else None
        builders = list(dict.fromkeys(e['implementer'] for e in mine
                                      if e.get('event_type') == 'implementation' and e.get('implementer')))
        reviews = [e for e in mine if e.get('event_type') == 'review_result']
        result.append({'part': number, 'seconds': seconds,
                       'implementers': builders or [start.get('implementer') or UNKNOWN],
                       'reviewers': list(dict.fromkeys(e['reviewer'] for e in reviews if e.get('reviewer'))),
                       'approved': end.get('event_type') in ('subtask_merged', 'base_merged') if end else None})
    return result


def build(events, project, task_id):
    """The report of one run (a dict of allowed values) from the events of its project."""
    own = [e for e in events if isinstance(e, dict) and e.get('project') == project
           and e.get('task_id') == task_id]
    own.sort(key=lambda e: str(e.get('timestamp', '')))

    def kind(name):
        return [e for e in own if e.get('event_type') == name]

    finished = next(iter(kind('run_finished')[-1:]), {})
    assigned = next(iter(kind('assigned')), {})
    # Who built and reviewed each part, including parts reused from a checkpoint of an earlier run.
    attribution = {}
    for summary in kind('subtasks_completed'):
        for item in summary.get('parts') if isinstance(summary.get('parts'), list) else []:
            if isinstance(item, dict) and _part(item) is not None:
                attribution[_part(item)] = item
    reviews = kind('review_result')
    outcome = finished.get('outcome') or UNKNOWN

    waves = kind('wave_timing')
    wave_seconds = {}
    for wave in waves:
        values = wave.get('seconds') if isinstance(wave.get('seconds'), list) else []
        for index, number in enumerate(wave.get('subtasks') or []):
            if isinstance(number, int) and not isinstance(number, bool):
                wave_seconds[number] = _number(values[index]) if index < len(values) else None
    numbers = sorted({_part(e) for e in own if _part(e) is not None} | set(wave_seconds))
    parts = []
    for number in numbers or [None]:
        mine = [e for e in own if _part(e) == number]
        if number is None:   # an unsplit task: its reviews and build are not tagged with a part
            mine = [e for e in own if e.get('event_type') in ('implementation', 'review_result')]
            if not mine:
                break
        completed = next((e for e in mine if e.get('event_type') == 'subtask_completed'), {})
        # A part reused from a verified checkpoint was approved by an independent review earlier.
        resumed = next((e for e in mine if e.get('event_type') == 'subtask_resumed'), {})
        attributed = attribution.get(number, {})
        built = next((e for e in mine if e.get('event_type') == 'implementation'), {})
        own_reviews = [e for e in mine if e.get('event_type') == 'review_result']
        builder = (completed.get('implementer') or resumed.get('implementer') or attributed.get('implementer')
                   or built.get('implementer') or next(
                       (e.get('implementer') for e in own_reviews if e.get('implementer')), None) or (
                       assigned.get('implementer') if number is None else None))
        reviewers = list(dict.fromkeys(e['reviewer'] for e in own_reviews if e.get('reviewer')))
        if not reviewers:
            reviewers = [r for r in (completed.get('reviewer') or resumed.get('reviewer')
                                     or attributed.get('reviewer'),) if r]
        if number in wave_seconds:
            seconds, parallel = wave_seconds[number], True
        elif number is None:
            seconds, parallel = _number(finished.get('duration_seconds')), False
        else:
            seconds, parallel = _part_seconds(mine), False
        parts.append({'part': number, 'implementer': builder, 'reviewers': reviewers,
                      'review_rounds': len(own_reviews), 'seconds': seconds, 'parallel': parallel,
                      'approved': bool(completed or resumed) or (number is None and outcome == 'approved')})

    rounds = _number(finished.get('review_rounds'))
    complete = rounds is None or rounds == len(reviews)
    cycled = [e for e in reviews if isinstance(e.get('cycle'), int)
              and not isinstance(e['cycle'], bool) and e['cycle'] >= 0]
    findings, seen = dict.fromkeys(SEVERITIES, 0), {}
    sessions, reviewed, next_cycle = {}, set(), {}
    # Every review loop must be stored from its cycle 0 on, without gaps; otherwise a missing first
    # review or its findings would silently count as zero.
    in_order = complete and len(cycled) == len(reviews)
    findings_known = bool(reviews) and in_order
    for event in own:
        number = _part(event)
        if event.get('event_type') == 'merge_conflict':
            sessions[number] = sessions.get(number, 0) + 1
            reviewed.discard(number)
        if event.get('event_type') != 'review_result':
            continue
        # IDs survive revisions, but each new review loop (cycle 0) starts its own namespace.
        if event.get('cycle') == 0 and number in reviewed:
            sessions[number] = sessions.get(number, 0) + 1
        reviewed.add(number)
        session = (number, sessions.get(number, 0))
        if in_order and event['cycle'] != next_cycle.get(session, 0):
            in_order = findings_known = False
        if in_order:
            next_cycle[session] = event['cycle'] + 1
        items = event.get('findings')
        if not isinstance(items, list):
            findings_known = False
            continue
        for finding in items:
            if (not isinstance(finding, dict) or finding.get('severity') not in findings
                    or not finding.get('id')):
                findings_known = False
                continue
            seen[(number, sessions.get(number, 0), str(finding['id']))] = finding['severity']
    for severity in seen.values():
        findings[severity] += 1
    rejected_first = (sum(1 for e in cycled if e['cycle'] == 0 and e.get('approved') is False)
                      if in_order and all(isinstance(e.get('approved'), bool) for e in cycled if e['cycle'] == 0)
                      and (reviews or rounds == 0) else None)

    gates = kind('pre_review_gate')
    tokens = {}
    for call in kind('agent_usage'):
        value = _number(call.get('billable_tokens'))
        # note_usage() writes billable_tokens=0 when the CLI reported no counts at all.
        counts = [call[field] for field in usage.USAGE_FIELDS if field in call]
        if counts and all(_number(count) is None for count in counts):
            value = None
        purpose = str(call.get('purpose') or UNKNOWN)
        if value is None or (purpose in tokens and tokens[purpose] is None):
            tokens[purpose] = None
        else:
            tokens[purpose] = tokens.get(purpose, 0) + int(value)
    waits = [_number(w.get('idle_seconds')) for w in waves]
    return {
        'project': project, 'task_id': task_id, 'outcome': outcome,
        'approved': finished.get('approved') if isinstance(finished.get('approved'), bool) else None,
        'started': assigned.get('timestamp'), 'finished': finished.get('timestamp'),
        'categories': [str(c) for c in assigned.get('task_categories') or []][:10],
        'duration_seconds': _number(finished.get('duration_seconds')),
        'parts': parts,
        'integrations': _integrations(own),
        'review_rounds': rounds if rounds is not None else (len(reviews) if reviews else None),
        'rejected_first_reviews': rejected_first,
        'findings': findings if findings_known else None,
        'gate': {'runs': len(gates) if gates else None,
                 'failed': sum(1 for g in gates if g.get('passed') is False) if gates
                 and all(isinstance(g.get('passed'), bool) for g in gates) else None,
                 'repairs': len(kind('gate_repair')) if gates or kind('gate_repair') else None},
        'tokens': tokens or None,
        'wait_seconds': round(sum(waits), 1) if waits and all(w is not None for w in waits) else None,
        'escalation': _escalation(own, outcome),
    }


def _show(value, unit=''):
    return UNKNOWN if value is None else f'{value}{unit}'


def cell(value):
    return str(value).replace('\\', '\\\\').replace('|', '\\|').replace('\n', ' ')


def _tokens_total(tokens):
    return sum(tokens.values()) if tokens and all(v is not None for v in tokens.values()) else None


def render(report):
    """Markdown with a machine-readable first line, so `ats.py report` can list it."""
    meta = {key: report[key] for key in ('project', 'task_id', 'outcome', 'approved', 'finished',
                                         'duration_seconds', 'review_rounds', 'rejected_first_reviews')}
    meta['parts'] = len(report['parts'])
    meta['findings'] = report['findings']
    meta['tokens'] = _tokens_total(report['tokens'])
    lines = [MARKER + json.dumps(meta, ensure_ascii=False).replace('--', '-\\u002d') + ' -->',
             f"# ATS run report {cell(report['task_id'])}", '',
             f"- Project: {cell(report['project'])}",
             f"- Outcome: {cell(report['outcome'])}",
             f"- Started: {cell(_show(report['started']))}",
             f"- Finished: {cell(_show(report['finished']))}",
             f"- Total duration: {_show(report['duration_seconds'], ' s')} (wall time; parallel parts counted once)",
             f"- Categories: {cell(', '.join(report['categories']) or UNKNOWN)}",
             '', '## Parts', '',
             '| Part | Implementer | Reviewers | Review rounds | Duration | Approved |',
             '| --- | --- | --- | --- | --- | --- |']
    for part in report['parts']:
        duration = _show(part['seconds'], ' s') + (' (parallel)' if part['parallel'] else '')
        lines.append(f"| {'task' if part['part'] is None else part['part']} | "
                     f"{cell(part['implementer'] or UNKNOWN)} | {cell(', '.join(part['reviewers']) or UNKNOWN)} | "
                     f"{part['review_rounds']} | {duration} | {'yes' if part['approved'] else 'no'} |")
    if not report['parts']:
        lines.append(f'| {UNKNOWN} | | | | | |')
    if report['integrations']:
        lines += ['', '## Integration', '',
                  '| Part | Implementers | Reviewers | Repair duration | Approved |',
                  '| --- | --- | --- | --- | --- |']
        for repair in report['integrations']:
            approved = UNKNOWN if repair['approved'] is None else ('yes' if repair['approved'] else 'no')
            lines.append(f"| {'base update' if repair['part'] is None else repair['part']} | "
                         f"{cell(', '.join(repair['implementers']))} | "
                         f"{cell(', '.join(repair['reviewers']) or UNKNOWN)} | "
                         f"{_show(repair['seconds'], ' s')} | {approved} |")
    findings = report['findings']
    gate = report['gate']
    lines += ['', '## Review', '',
              f"- Review rounds: {_show(report['review_rounds'])}",
              f"- Rejected first reviews: {_show(report['rejected_first_reviews'])}",
              '- Findings: ' + (', '.join(f'{s} {findings[s]}' for s in SEVERITIES) if findings else UNKNOWN),
              f"- Pre-review gate: {_show(gate['runs'])} runs, {_show(gate['failed'])} failed, "
              f"{_show(gate['repairs'])} repairs",
              '', '## Tokens and waiting', '']
    if report['tokens']:
        lines += [f'- {cell(purpose)}: {_show(count)}' for purpose, count in sorted(report['tokens'].items())]
        lines.append(f"- Total: {_show(_tokens_total(report['tokens']))}")
    else:
        lines.append(f'- Tokens: {UNKNOWN}')
    lines.append('- Wait time in parallel waves: ' + (
        f"{report['wait_seconds']} s" if report['wait_seconds'] is not None else UNKNOWN))
    lines += ['', '## Escalation', '']
    lines += [f'- {cell(reason)}' for reason in report['escalation']] or ['- none']
    return '\n'.join(lines) + '\n'


def report_path(data, project, task_id):
    safe = re.sub(r'[^a-zA-Z0-9_-]', '-', str(task_id))[:70] or 'run'
    return Path(data) / project / 'reports' / f'{safe}.md'


def write(data, project, task_id, events):
    """Write DATA/<project>/reports/<task_id>.md and return its path."""
    path = report_path(data, project, task_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(render(build(events, project, task_id)), encoding='utf-8')
    return path


def read_meta(path):
    try:
        first = Path(path).read_text(encoding='utf-8').split('\n', 1)[0]
    except (OSError, UnicodeDecodeError):
        return None
    if not (first.startswith(MARKER) and first.endswith(' -->')):
        return None
    try:
        meta = json.loads(first[len(MARKER):-4])
    except ValueError:
        return None
    return meta if isinstance(meta, dict) else None


def recent(data, project, days, now=None):
    """Metadata of this project's reports whose run finished in the last `days` days, newest first."""
    now = now or dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=days)
    folder = Path(data) / project / 'reports'
    rows = []
    for path in sorted(folder.glob('*.md')) if folder.is_dir() else ():
        meta = read_meta(path)
        finished = _time(meta.get('finished')) if meta else None
        if meta and meta.get('project') == project and finished and since <= finished <= now:
            rows.append(meta)
    return sorted(rows, key=lambda m: _time(m['finished']), reverse=True)


def table(rows, days):
    if not rows:
        return f'No run reports in the last {days} days.'
    lines = ['| Finished (UTC) | Task | Outcome | Duration | Parts | Review rounds | Rejected first | '
             'Findings B/M/m/S | Tokens |',
             '| --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for row in rows:
        findings = row.get('findings')
        counts = '/'.join(str(findings.get(s, 0)) for s in SEVERITIES) if isinstance(findings, dict) else UNKNOWN
        finished = _time(row['finished']).astimezone(dt.timezone.utc).strftime('%Y-%m-%d %H:%M')
        lines.append(f"| {finished} | {cell(str(row.get('task_id'))[:8])} | {cell(row.get('outcome') or UNKNOWN)} | "
                     f"{cell(_show(row.get('duration_seconds'), ' s'))} | {cell(_show(row.get('parts')))} | "
                     f"{cell(_show(row.get('review_rounds')))} | {cell(_show(row.get('rejected_first_reviews')))} | "
                     f"{cell(counts)} | {cell(_show(row.get('tokens')))} |")
    return '\n'.join(lines)
