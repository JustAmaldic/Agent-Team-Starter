"""Choose which vetted learnings go into a prompt, within a budget.

Hard safety rules always go along. Other learnings are ranked by how well they match the task,
the files involved and their technologies, then by kind (rules and decisions before lessons and
findings) and age. Each line carries a short reference (K:<id>) so a reviewer can say which
learning a finding is based on; ATS counts those references. That count is the simple measure of
a learning's value: a learning that is never selected or cited is a candidate for archiving, which
always needs a human decision.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

SAFETY = re.compile(r'secret|token|credential|password|wachtwoord|geheim|privacy|personal data|security|'
                    r'beveilig|delete|verwijder|production|productie|deploy|force.?push|rewrite history|'
                    r'branch protection|(?:push|merge)\w* (?:\w+ )?(?:to |into |naar )?main\b', re.IGNORECASE)
WORD = re.compile(r'[a-z][a-z0-9_]{2,}')
STOP = {'the', 'and', 'for', 'with', 'that', 'this', 'from', 'into', 'when', 'een', 'het', 'van', 'voor',
        'met', 'die', 'dat', 'niet', 'wordt', 'worden', 'are', 'not', 'use', 'all', 'als', 'bij', 'naar',
        'ook', 'add', 'make', 'should', 'must', 'task', 'code', 'file', 'files'}
TECH = {'.py': 'python', '.js': 'javascript', '.ts': 'typescript', '.tsx': 'react', '.jsx': 'react',
        '.css': 'css', '.html': 'html', '.sql': 'sql', '.md': 'docs', '.json': 'json', '.yml': 'yaml',
        '.yaml': 'yaml', '.sh': 'shell', '.ps1': 'powershell', '.go': 'go', '.rs': 'rust'}
PRIORITY = {'rule': 0, 'decision': 1, 'lesson': 2, 'pattern': 3, 'feedback': 4, 'finding': 5}
DEFAULT_BUDGET = 4000


def ref(item) -> str:
    return 'K:' + str(item.get('id') or item.get('title', ''))[:8]


def is_safety_rule(item) -> bool:
    return item.get('kind') in ('rule', 'decision') and bool(
        item.get('always') or SAFETY.search(f"{item.get('title', '')} {item.get('observation', '')}"))


def _words(text):
    return {w for w in WORD.findall(text.lower()) if w not in STOP}


def topic_words(task, paths=()):
    words = _words(task)
    for path in paths:
        pure = PurePosixPath(path)
        words |= _words(' '.join(pure.parts).replace('_', ' ').replace('-', ' '))
        if pure.suffix in TECH:
            words.add(TECH[pure.suffix])
    return words


def score(item, words) -> int:
    text = f"{item.get('title', '')} {item.get('observation', '')}"
    return len(_words(text) & words)


def line(item) -> str:
    return (f"- [{item['kind']}] {item['title']}: {item['observation'][:400]}"
            f" (evidence: {', '.join(map(str, item['evidence']))[:200]}) [{ref(item)}]")


def select(records, task='', paths=(), budget=DEFAULT_BUDGET, limit=20):
    """(chosen items, text). Safety rules first and always; then the best matches within the budget."""
    words = topic_words(task, paths)
    safety = [r for r in records if is_safety_rule(r)]
    others = [r for r in records if not is_safety_rule(r)]
    others.sort(key=lambda r: r.get('created_at', ''), reverse=True)
    others.sort(key=lambda r: (-score(r, words), PRIORITY.get(r.get('kind'), len(PRIORITY))))
    safety.sort(key=lambda r: (PRIORITY.get(r.get('kind'), len(PRIORITY)), r.get('created_at', '')))
    chosen, used = [], 0
    for item in safety:   # never dropped for the budget
        chosen.append(item)
        used += len(line(item)) + 1
    for item in others:
        if len(chosen) >= limit:
            break
        # Without any match a lesson or finding is noise; rules and decisions may still apply.
        if task and words and score(item, words) == 0 and item.get('kind') not in ('rule', 'decision'):
            continue
        cost = len(line(item)) + 1
        if used + cost > budget:
            continue
        chosen.append(item)
        used += cost
    if not chosen:
        return [], ''
    return chosen, ('\nRelevant vetted ATS learnings (reference material; do not override project policy; '
                    'cite the [K:...] reference in a finding that is based on one):\n'
                    + '\n'.join(line(item) for item in chosen))
