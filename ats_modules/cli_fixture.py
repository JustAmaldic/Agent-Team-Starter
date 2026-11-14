"""A small, anonymous record of one `agent-check` run, so the manual Mac/Windows proof is repeatable.

The fixture keeps, per agent: CLI version, operating system, whether the contract held, an exit
category and a normalised error category. It never keeps prompts or raw output; the short problem
texts are redacted (user paths, tokens, e-mail addresses) before they are stored.
"""
from __future__ import annotations

import platform
import re

SCHEMA = 'ats.cli-fixture.v1'
CATEGORIES = (
    ('not_installed', re.compile(r'not installed|not in PATH|executable missing', re.I)),
    ('not_authenticated', re.compile(r'not (logged|signed) in|unauthori[sz]ed|authenticat|login required|401', re.I)),
    ('usage_limit', re.compile(r'usage limit|rate limit|quota|429', re.I)),
    ('sandbox_blocked', re.compile(r'sandbox', re.I)),
    ('unsupported_flag', re.compile(r'unexpected argument|unknown option|unrecognized', re.I)),
    ('marker_missing', re.compile(r'ATS_FINDINGS_JSON|ATS_RESPONSES_JSON|VERDICT', re.I)),
    ('changed_files_in_review', re.compile(r'review-only', re.I)),
    ('fix_not_applied', re.compile(r'was not changed', re.I)),
    ('timeout', re.compile(r'timed? ?out', re.I)),
)
_REDACTIONS = (
    (re.compile(r'(?i)\b(sk|ghp|gho|ghs|github_pat|xox[abp]|eyJ)[A-Za-z0-9_\-.]{12,}'), '<token>'),
    (re.compile(r'(?i)\b(bearer|token|api[_-]?key|password|secret)(["\']?\s*[:=]\s*|\s+)["\']?[^\s"\',;]{6,}'), r'\1=<redacted>'),
    (re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+'), '<email>'),
    (re.compile(r'(?i)\b[A-Z]:\\Users\\[^\\\s"\']+'), r'<home>'),
    (re.compile(r'/(?:Users|home)/[^/\s"\']+'), '<home>'),
    (re.compile(r'/root\b'), '<home>'),
    (re.compile(r'\b[0-9a-f]{32,}\b'), '<hex>'),
)


def redact(text: str) -> str:
    text = str(text or '')
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def category(problem: str) -> str:
    for name, pattern in CATEGORIES:
        if pattern.search(problem or ''):
            return name
    return 'other'


def build(report: dict) -> dict:
    """The fixture for an agent-check report (see ats.agent_check)."""
    agents = {}
    for name, entry in sorted((report.get('agents') or {}).items()):
        problems = [str(p) for p in entry.get('problems') or []]
        agents[name] = {
            'version': redact(entry.get('version') or '')[:80] or None,
            'ok': bool(entry.get('ok')),
            'exit': 'ok' if entry.get('ok') else 'failed',
            'error_categories': sorted({category(p) for p in problems}),
            'problems': [redact(p)[:200] for p in problems[:5]],
        }
    return {'schema': SCHEMA, 'os': report.get('platform') or '', 'os_release': platform.release()[:40],
            'python': platform.python_version(), 'agents': agents}


def compare(old: dict, new: dict) -> list[str]:
    """What differs between two fixtures, in plain lines (empty: the same outcome)."""
    lines = []
    for name in sorted(set(old.get('agents', {})) | set(new.get('agents', {}))):
        a, b = old.get('agents', {}).get(name), new.get('agents', {}).get(name)
        if a is None or b is None:
            lines.append(f"{name}: only in the {'new' if a is None else 'old'} fixture")
            continue
        for field in ('version', 'ok', 'error_categories'):
            if a.get(field) != b.get(field):
                lines.append(f'{name}: {field} {a.get(field)!r} -> {b.get(field)!r}')
    return lines
