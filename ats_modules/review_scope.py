"""How much review a change gets: review depth (light, standard, deep) and delta or full re-review.

Depth follows risk and size: a small change to docs, tests or text gets a light review with a
checklist, a high-risk change (ats_intelligence.risk_profile) always a deep one (normal review plus
a focused security and data-safety pass). Depth can be raised per project, never lowered below
what the risk requires.

From the second review round on, the reviewer gets the changes since its previous review plus the
open findings, unless the new changes are large, touch dependencies or security-sensitive code,
or look like a refactor: then it reviews the full diff again.
"""
from __future__ import annotations

from pathlib import PurePosixPath

LEVELS = ('light', 'standard', 'deep')
DEPENDENCY_FILES = {
    'requirements.txt', 'requirements-dev.txt', 'pyproject.toml', 'setup.py', 'setup.cfg', 'Pipfile',
    'Pipfile.lock', 'poetry.lock', 'uv.lock', 'package.json', 'package-lock.json', 'yarn.lock',
    'pnpm-lock.yaml', 'go.mod', 'go.sum', 'Cargo.toml', 'Cargo.lock', 'Gemfile', 'Gemfile.lock',
    'composer.json', 'composer.lock', '.gitmodules',
}
LIGHT_SUFFIXES = ('.md', '.rst', '.txt', '.po', '.pot')
LIGHT_PARTS = {'tests', 'test', 'docs', 'doc', 'locales', 'locale', 'i18n'}


def diff_stats(diff: str) -> dict:
    files, added, removed, renamed = [], 0, 0, 0
    for line in diff.splitlines():
        if line.startswith('+++ '):
            target = line[4:].strip()
            if target != '/dev/null':
                files.append(target[2:] if target.startswith('b/') else target)
        elif line.startswith('rename from '):
            renamed += 1
        elif line.startswith('+') and not line.startswith('+++'):
            added += 1
        elif line.startswith('-') and not line.startswith('---'):
            removed += 1
    return {'files': files, 'added': added, 'removed': removed, 'renamed': renamed,
            'lines': added + removed}


def is_light_path(path: str) -> bool:
    pure = PurePosixPath(path)
    return (pure.suffix.lower() in LIGHT_SUFFIXES or bool(set(pure.parts[:-1]) & LIGHT_PARTS)
            or pure.name.startswith('test_') or pure.stem.endswith('_test'))


def dependency_change(paths) -> list[str]:
    return [p for p in paths if PurePosixPath(p).name in DEPENDENCY_FILES]


def review_level(task: str, stats: dict, risk: dict, conf: dict) -> tuple[str, str]:
    """(level, reason). High risk is always deep; the project minimum can only raise it."""
    minimum = conf.get('review_level_min', 'light')
    minimum = minimum if minimum in LEVELS else 'light'
    if risk.get('level') == 'high':
        return 'deep', 'high risk: ' + ', '.join(risk.get('reasons') or ['sensitive change'])
    limit = int(conf.get('light_review_max_lines', 80))
    if (stats['files'] and stats['lines'] <= limit and all(is_light_path(p) for p in stats['files'])
            and not dependency_change(stats['files'])):
        level, reason = 'light', f"{stats['lines']} changed lines in docs, tests or text only"
    else:
        level, reason = 'standard', f"{stats['lines']} changed lines in {len(stats['files'])} files"
    if LEVELS.index(minimum) > LEVELS.index(level):
        return minimum, f'project minimum is {minimum} ({reason})'
    return level, reason


def rereview_mode(cycle: int, delta: dict, full: dict, risk: dict, conf: dict) -> tuple[str, str]:
    """('delta' | 'full', reason) for review round `cycle` (0 is the first review)."""
    if cycle == 0:
        return 'full', 'first review'
    if not conf.get('delta_review', True):
        return 'full', 'delta review disabled for this project'
    if not delta['files']:
        return 'full', 'no changes since the previous review'
    deps = dependency_change(delta['files'])
    if deps:
        return 'full', 'dependency files changed: ' + ', '.join(deps[:5])
    if risk.get('level') == 'high':
        return 'full', 'security-sensitive changes: ' + ', '.join(risk.get('reasons') or [])
    limit = int(conf.get('delta_review_max_lines', 400))
    if delta['lines'] > limit or (full['lines'] and delta['lines'] > 0.6 * full['lines']):
        return 'full', f"large revision ({delta['lines']} of {full['lines']} changed lines)"
    if delta['renamed'] >= 5 or len(delta['files']) >= 10:
        return 'full', f"refactor-like revision ({len(delta['files'])} files, {delta['renamed']} renames)"
    return 'delta', f"{delta['lines']} changed lines in {len(delta['files'])} files since the previous review"
