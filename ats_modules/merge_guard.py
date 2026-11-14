"""Nothing that is needed may disappear when a merge conflict is resolved.

A resolution that simply takes one side drops what the other side added. For every file both
sides changed, this compares the names the code defines (functions, classes, methods, tests,
constants, routes) on each side with the resolved file:

- a name that exists on either side must still exist after the resolution,
- unless one side removed it on purpose (it was in the common ancestor and that side dropped it).

The result is a list for the reviewer and the fixer, not a verdict: a rename is reported too.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

CODE_SUFFIXES = {'.py', '.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.vue', '.svelte', '.go', '.rs', '.java',
                 '.kt', '.cs', '.rb', '.php', '.lua', '.luau', '.sh', '.css', '.scss', '.html'}
PATTERNS = (
    re.compile(r'^[ \t]*(?:async[ \t]+)?def[ \t]+([A-Za-z_]\w*)', re.M),                       # python
    re.compile(r'^[ \t]*class[ \t]+([A-Za-z_]\w*)', re.M),
    re.compile(r'^([A-Z][A-Z0-9_]{2,})[ \t]*(?::[^=\n]+)?=[^=]', re.M),                          # constants
    re.compile(r'^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?(?:async[ \t]+)?function\*?[ \t]+([A-Za-z_$][\w$]*)', re.M),
    re.compile(r'^[ \t]*(?:export[ \t]+)?(?:const|let|var)[ \t]+([A-Za-z_$][\w$]*)[ \t]*=', re.M),
    re.compile(r'^[ \t]*(?:export[ \t]+)?(?:interface|type|enum)[ \t]+([A-Za-z_$][\w$]*)', re.M),
    re.compile(r'^[ \t]*func[ \t]+(?:\([^)]*\)[ \t]*)?([A-Za-z_]\w*)', re.M),                    # go
    re.compile(r'^[ \t]*(?:pub[ \t]+)?fn[ \t]+([A-Za-z_]\w*)', re.M),                            # rust
    re.compile(r'''@\w+(?:\.\w+)*\.(?:get|post|put|patch|delete|route)\(\s*["'](/[^"']*)["']''', re.M),   # routes
    re.compile(r'''\b(?:it|test|describe)\(\s*["'`]([^"'`\n]{3,80})["'`]''', re.M),              # js tests
    re.compile(r'''\bid=["']([\w-]{3,})["']''', re.M),                                          # html ids
    re.compile(r'^[ \t]*([.#][\w-]{3,})[ \t]*[{,]', re.M),                                       # css selectors
)
MAX_BYTES = 600000


def is_code(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in CODE_SUFFIXES


def symbols(text: str | None) -> set[str]:
    if not text:
        return set()
    text = text[:MAX_BYTES]
    found = set()
    for pattern in PATTERNS:
        found.update(pattern.findall(text))
    return found


def lost(ancestor: str | None, ours: str | None, theirs: str | None, resolved: str | None) -> list[str]:
    """Names that exist on a side but not in the resolution, and that no side removed on purpose."""
    base, a, b, now = symbols(ancestor), symbols(ours), symbols(theirs), symbols(resolved)
    removed_on_purpose = (base - a) | (base - b)
    return sorted((a | b) - now - removed_on_purpose)


def check(files: dict[str, tuple]) -> list[dict]:
    """files: {path: (ancestor, ours, theirs, resolved)} -> [{'file', 'lost': [...]}] for code files."""
    result = []
    for path, versions in sorted(files.items()):
        if not is_code(path):
            continue
        missing = lost(*versions)
        if versions[3] is None and (versions[1] is not None and versions[2] is not None):
            missing = ['(the whole file)']   # both sides kept the file, the resolution deleted it
        if missing:
            result.append({'file': path, 'lost': missing[:40]})
    return result


def note(found) -> str:
    if not found:
        return ''
    return ('MERGE GUARD: after the conflict resolution these names, which exist on one of the two sides, are no '
            'longer in the file: '
            + '; '.join(f"{f['file']}: {', '.join(f['lost'][:12])}" for f in found[:10])
            + '. For each one: verify in the code whether it was renamed or moved (fine) or dropped (a BLOCKER: '
            'the resolution must keep the functionality of both sides).')
