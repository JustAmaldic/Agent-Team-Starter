"""Write targets outside the worktree, found in the task text before any agent starts.

Agents only ever change files inside the task's worktree. A task that asks to write somewhere else
on the computer ("save it in ~/Desktop", "append to /etc/hosts", "put it in ..\\other-repo") cannot be
done within that rule, so ATS stops before spending tokens on it and says so.

Only real computer locations count: the home folder, system and temp folders, drive letters,
environment folders and paths that climb out of the project with "..". Web routes such as /api/users
are not file paths. Reading from such a place is allowed; only sentences that ask to write are blocked.
Events and logs get the path *category* only, never the path itself (it can hold a user name).
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

CATEGORIES = ('home', 'system', 'temp', 'other_drive', 'environment', 'parent')
WRITE_WORDS = re.compile(
    r'\b(write|writes|save|saves|store|create|creates|add|append|put|place|move|copy|install|delete|remove|'
    r'overwrite|modify|edit|change|update|export|dump|generate|touch|mkdir|'
    r'schrijf|schrijft|bewaar|sla|opslaan|maak|zet|plaats|verplaats|kopieer|installeer|verwijder|wis|'
    r'overschrijf|wijzig|bewerk|pas|exporteer|genereer)\b', re.IGNORECASE)
PATH = re.compile(
    r'(?<![\w/.:@-])('
    r'~[/\\][^\s"\'`<>|]*'
    r'|\$(?:HOME|TMPDIR|TEMP|USERPROFILE|APPDATA|LOCALAPPDATA)\b[^\s"\'`<>|]*'
    r'|%(?:USERPROFILE|APPDATA|LOCALAPPDATA|TEMP|TMP|PROGRAMFILES|WINDIR)%[^\s"\'`<>|]*'
    r'|[A-Za-z]:[\\/][^\s"\'`<>|]*'
    r'|/(?:home|Users|root|etc|usr|var|opt|bin|sbin|lib|tmp|private|Library|System|Applications|mnt|Volumes|srv|boot|dev|proc)'
    r'(?:/[^\s"\'`<>|]*)?'
    r'|(?:\.\.[/\\])+[^\s"\'`<>|]*'
    r')')
SENTENCE = re.compile(r'[^.!?\n;]*(?:[.!?](?=\S)[^.!?\n;]*)*')


def category(path: str) -> str:
    if path.startswith('~'):
        return 'home'
    if path.startswith(('$', '%')):
        return 'temp' if re.match(r'[$%](TMPDIR|TEMP|TMP)\b', path) else 'environment'
    if path.startswith('..'):
        return 'parent'
    if re.match(r'[A-Za-z]:', path):
        rest = path[2:].replace('\\', '/').lower()
        if rest.startswith('/users/'):
            return 'home'
        return 'system' if rest.startswith(('/windows', '/program files', '/programdata')) else 'other_drive'
    top = PurePosixPath(path).parts[1] if len(PurePosixPath(path).parts) > 1 else ''
    if top in ('home', 'Users', 'root'):
        return 'home'
    if top in ('tmp', 'private'):
        return 'temp'
    return 'system'


def _inside(path: str, worktree) -> bool:
    """An absolute path that points into the project itself is fine."""
    if worktree is None or path.startswith(('~', '$', '%', '..')):
        return False
    try:
        root = Path(worktree).resolve()
        target = Path(path.rstrip('.,;:)]}'))
        return target.is_absolute() and (target == root or root in target.parents)
    except (OSError, ValueError):
        return False


def external_targets(task: str, worktree=None) -> list[str]:
    """Categories of the outside locations the task asks to write to (sorted, unique)."""
    found = set()
    for sentence in SENTENCE.findall(task or ''):
        if not sentence.strip() or not WRITE_WORDS.search(PATH.sub(' ', sentence)):
            continue
        for match in PATH.finditer(sentence):
            path = match.group(1)
            if not _inside(path, worktree):
                found.add(category(path))
    return sorted(found, key=CATEGORIES.index)


def reason(categories) -> str:
    names = {'home': 'the home folder', 'system': 'a system folder', 'temp': 'a temp folder',
             'other_drive': 'another drive or folder', 'environment': 'a user environment folder',
             'parent': 'a folder above the project'}
    where = ', '.join(names[c] for c in categories)
    return (f'the task asks to write outside the project ({where}). Agents only change files inside the '
            'worktree. Rephrase the task so the result lands in the project, or do that step yourself '
            '(write_scope: "warn" in .ats-project.json turns this stop into a warning)')
