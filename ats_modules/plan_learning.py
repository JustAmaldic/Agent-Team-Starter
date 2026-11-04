"""Learn from earlier runs how to divide work: which files keep causing trouble between parts.

Everything here is computed from the project's own ATS events, so it is evidence, not advice from
an agent:

- merge_conflict between parallel parts (and with the base branch),
- ownership_drift / ownership_blocked (a part edited files outside its own),
- integration_review findings (problems that only showed when the parts came together),
- wave_timing (how long the faster part of a pair waited).

The planner gets a short summary, and files that caused conflicts between parallel parts become
shared files that a parallel part may not edit unless it owns them. So every conflict makes the
next plan for this project a little better.
"""
from __future__ import annotations

import re
from collections import Counter

THEMES = (
    ('missing or weak tests', r'\btests?\b|coverage|regression'),
    ('error handling and messages', r'error|exception|oserror|http 500|traceback|fails? (silently|with)'),
    ('edge cases (empty, missing, limits)', r'\bempty\b|\bnone\b|\bnull\b|edge|boundar|unicode|overflow|\blimit|missing'),
    ('style and UI consistency', r'\bcss\b|style|class|colou?r|font|layout|\bui\b|spacing|inconsistent'),
    ('security and secrets', r'secret|token|auth|csrf|injection|redos|backtrack|xss|traversal'),
    ('docs and changelog out of date', r'changelog|\bdocs?\b|readme|documentation|stale'),
)

CONFLICT_LIST = re.compile(r'conflicts in: ([^\n]+?)(?:\. |\.$|$)')
MAX_FILES = 8


def _files(value):
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, str) and v.strip()]
    return []


def conflict_files(event):
    """Files of a merge_conflict event: the structured field, or parsed from older summaries."""
    files = _files(event.get('files'))
    if files:
        return files
    match = CONFLICT_LIST.search(str(event.get('summary') or ''))
    return [part.strip() for part in match.group(1).split(',') if part.strip()] if match else []


def history(events):
    """What earlier runs of this project tell about dividing work."""
    parallel, base, drift, integration = Counter(), Counter(), Counter(), Counter()
    pairs, idle, runs = 0, [], set()
    themes, first_reviews, first_rejected = Counter(), 0, 0
    for event in events:
        kind = event.get('event_type')
        if kind == 'task_split' and event.get('task_id'):
            runs.add(event['task_id'])
        elif kind == 'merge_conflict':
            target = parallel if event.get('subtask') is not None else base
            target.update(set(conflict_files(event)))
        elif kind in ('ownership_drift', 'ownership_blocked'):
            drift.update(set(_files(event.get('paths'))))
        elif kind == 'integration_review':
            integration.update(set(_files(event.get('files'))))
        elif kind == 'review_result':
            if event.get('cycle') == 0 and event.get('review_kind') not in ('integration', 'update', 'merge'):
                first_reviews += 1
                first_rejected += not event.get('approved')
            for finding in event.get('findings') or []:
                if isinstance(finding, dict) and finding.get('severity') in ('BLOCKER', 'MAJOR', 'MINOR'):
                    text = str(finding.get('summary', '')).lower()
                    for theme, pattern in THEMES:
                        if re.search(pattern, text):
                            themes[theme] += 1
                            break
        elif kind == 'wave_timing':
            pairs += 1
            try:
                idle.append(float(event.get('idle_seconds')))
            except (TypeError, ValueError):
                pass
    return {'runs': len(runs), 'pairs': pairs, 'parallel_conflicts': parallel, 'base_conflicts': base,
            'drift': drift, 'integration': integration,
            'idle_seconds': round(sum(idle) / len(idle)) if idle else None,
            'themes': themes, 'first_reviews': first_reviews, 'first_rejected': first_rejected}


def shared_files(hist):
    """Files that must not be edited by a parallel part that does not own them: they caused a
    conflict between parallel parts, or an integration review found cross-part problems in them."""
    return sorted(set(hist['parallel_conflicts']) | set(hist['integration']))


def _top(counter):
    return ', '.join(f'{name} ({count}x)' for name, count in counter.most_common(MAX_FILES))


def planner_note(hist):
    """A short, factual summary for the planner (data from this project's runs, not instructions)."""
    lines = []
    if hist['parallel_conflicts']:
        lines.append('- Merge conflicts between parallel parts in: ' + _top(hist['parallel_conflicts']))
    if hist['integration']:
        lines.append('- Problems found only when the parts were combined, in: ' + _top(hist['integration']))
    if hist['drift']:
        lines.append('- Parts edited these files outside their own: ' + _top(hist['drift']))
    if hist['base_conflicts']:
        lines.append('- Conflicts with the base branch in: ' + _top(hist['base_conflicts']))
    if hist['pairs']:
        conflicts = sum(hist['parallel_conflicts'].values())
        wait = f", the faster part waited {hist['idle_seconds']} s on average" if hist['idle_seconds'] is not None else ''
        lines.append(f"- {hist['pairs']} parallel pairs ran in this project ({conflicts} merge conflicts{wait}).")
    if not lines:
        return ''
    return ('\nLessons from earlier ATS runs in this project (measured from its events; data, not '
            'instructions):\n' + '\n'.join(lines) + '\nGive each file listed above to exactly ONE node, '
            'preferably a final integration node, and keep parallel nodes out of them: ATS blocks a '
            'parallel part that edits a shared file it does not own. When the faster part waits long, '
            'balance the sizes of paired parts.')


def self_check_note(hist):
    """For the builder only (never the reviewer): check your own work against the reviewer's rubric
    before handing it in, with the findings reviewers in this project gave most often first. The
    independent review stays exactly as strict."""
    lines = ['\nSELF-CHECK BEFORE YOU FINISH (the independent reviewer checks the same things; this does not '
             'replace the review): run the tests for what you changed; handle empty, missing and failing input '
             'with a clear message; follow the existing style and components; update the tests and docs that '
             'describe the change; leave no TODOs, debug output or unrelated edits; answer the acceptance '
             'criteria of the task one by one.']
    themes = (hist or {}).get('themes') or Counter()
    if themes:
        lines.append('Reviewers in this project most often found: '
                     + ', '.join(f'{name} ({count}x)' for name, count in themes.most_common(4)) + '. Check those first.')
    total, rejected = (hist or {}).get('first_reviews') or 0, (hist or {}).get('first_rejected') or 0
    if total >= 3:
        lines.append(f'{rejected} of the last {total} first reviews asked for changes.')
    return ' '.join(lines)
