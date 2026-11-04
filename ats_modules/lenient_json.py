"""Read the JSON on an agent's marker line, also when the agent forgot to escape a quote.

Agents copy text from the task into JSON strings. A task that says "Nieuwe run" (with double
quotes) then often ends up as `"title": "Koppel het "Nieuwe run"-formulier"`, which is not
valid JSON. One such quote cost a whole planning run twice in a row. This module repairs only
that mistake: a double quote inside a string that cannot be the end of the string is escaped.
Anything else that is invalid stays invalid and raises, with a short excerpt around the error
so the agent can be told exactly what to fix.
"""
from __future__ import annotations

import json

# After a real closing quote JSON continues with one of these (after optional spaces).
_CLOSERS = set(',:}]')
# After `",` a new value or key starts with one of these; Dutch or English text does not.
_AFTER_COMMA = set('"{[]}0123456789-')


def _closes(text: str, index: int) -> bool:
    """Is the quote at `index` (inside a string) the end of that string?"""
    rest = text[index + 1:].lstrip()
    if not rest:
        return True
    if rest[0] not in _CLOSERS:
        return False
    if rest[0] == ',':
        after = rest[1:].lstrip()
        return not after or after[0] in _AFTER_COMMA
    return True


def repair_quotes(text: str) -> str:
    """Escape double quotes that sit inside a JSON string instead of ending it."""
    out, in_string, escaped = [], False, False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                if _closes(text, index):
                    in_string = False
                else:
                    out.append('\\')
        elif char == '"':
            in_string = True
        out.append(char)
    return ''.join(out)


def excerpt(text: str, position: int, width: int = 60) -> str:
    start = max(0, position - width)
    return text[start:position] + ' >>>HERE>>> ' + text[position:position + width]


def loads(text: str):
    """json.loads, with a repair of unescaped inner quotes when the plain parse fails.

    Raises ValueError with the position and an excerpt when the text is still not valid.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError as first:
        repaired = repair_quotes(text)
        if repaired != text:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass
        raise ValueError(f'{first.msg} at character {first.pos}: {excerpt(text, first.pos)}. '
                         'Escape double quotes inside JSON strings as \\" or use single quotes.') from first
