"""Cheap, deterministic style checks on the added lines of UI files, and the project's UI vocabulary.

Agents got the colour tokens, but not how screens are built, and nothing checked the result. This
module gives two things:

- `gate_result`: a pre-review gate check that fails on a literal colour (hex, rgb, hsl) or a new
  font-family in an added line of a UI source file, when the project has design tokens to use
  instead. Token definitions themselves (`--name: #123456`) are allowed.
- `vocabulary`: the CSS classes and UI helper functions the project uses most, for the style brief,
  so agents build with what is there.
- `unknown_classes`: classes used in added lines that no stylesheet of the project defines; a note
  for the reviewer (not a failure: a class can be a JavaScript hook).
"""
from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path

UI_SUFFIXES = ('.css', '.scss', '.less', '.js', '.jsx', '.ts', '.tsx', '.vue', '.svelte', '.html')
SKIP_DIRS = {'node_modules', '.git', 'dist', 'build', '.venv', 'venv', '.venv-bouw', 'vendor', '__pycache__',
             '.next', 'coverage', 'site-packages', 'tests', 'test', '__tests__'}
COLOUR = re.compile(r'(?<![\w&-])#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b|\b(?:rgba?|hsla?)\(\s*\d')
FONT = re.compile(r'font-family\s*:|fontFamily\s*:')
TOKEN_DEFINITION = re.compile(r'^\s*--[\w-]+\s*:')
STYLE_CONTEXT = re.compile(r'\b(colou?r|background|border|fill|stroke|shadow|outline|style)\b', re.IGNORECASE)
CSS_CLASS = re.compile(r'\.(-?[A-Za-z_][\w-]*)')
USED_CLASS = re.compile(r'''\bclass(?:Name)?\s*[:=]\s*["'`]([^"'`$]{1,200})["'`]''')
HELPER_CALL = re.compile(r'\b([a-z][A-Za-z0-9]{1,30})\(')
HELPER_DEF = re.compile(r'\bfunction\s+([a-z][A-Za-z0-9]{1,30})\s*\(|\bconst\s+([a-z][A-Za-z0-9]{1,30})\s*=\s*\(')
MAX_FILES = 400


def _ui_file(path):
    parts = Path(path).parts
    return path.endswith(UI_SUFFIXES) and not path.endswith('.min.css') and not (set(parts[:-1]) & SKIP_DIRS)


def _files(folder, suffixes):
    found = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')]
        for name in files:
            if name.endswith(suffixes) and not name.endswith('.min.css'):
                found.append(Path(root) / name)
                if len(found) >= MAX_FILES:
                    return found
    return found


def _read(path):
    try:
        return path.read_text(encoding='utf-8', errors='replace')[:300000]
    except OSError:
        return ''


def has_tokens(folder):
    return any(':root' in _read(p) and '--' in _read(p) for p in _files(folder, ('.css', '.scss', '.less')))


def literal_values(added):
    """{path: [lines]} of added UI lines with a literal colour or a new font-family."""
    problems = {}
    for path, lines in added.items():
        if not _ui_file(path):
            continue
        stylesheet = path.endswith(('.css', '.scss', '.less'))
        for line in lines:
            if TOKEN_DEFINITION.match(line):
                continue   # defining a design token is how new colours should arrive
            text = re.sub(r'var\([^)]*\)', '', line)
            if stylesheet:
                # Declarations only: `#fade {` is a selector, not a colour.
                declaration = ':' in text and not text.rstrip().endswith('{')
                hit = declaration and COLOUR.search(text.split(':', 1)[1])
            else:
                # In scripts and markup only where a style is set: '#add' can be an element id.
                hit = STYLE_CONTEXT.search(text) and COLOUR.search(text)
            if hit or FONT.search(text):
                problems.setdefault(path, []).append(line.strip()[:160])
    return problems


def gate_result(folder, added, result, passed, failed, skipped):
    """The pre-review gate check (statuses from gate.py)."""
    ui = [p for p in added if _ui_file(p)]
    if not ui:
        return result('style', 'fast', skipped, 'no UI files changed')
    if not has_tokens(folder):
        return result('style', 'fast', skipped, 'the project defines no design tokens')
    problems = literal_values(added)
    if not problems:
        return result('style', 'fast', passed, f'{len(ui)} UI files')
    detail = '; '.join(f"{path}: {', '.join(lines[:3])}" for path, lines in list(problems.items())[:6])
    return result('style', 'fast', failed,
                  'Literal colours or fonts in added UI lines; use the project\'s design tokens (var(--...)) and '
                  'existing classes instead: ' + detail)


def defined_classes(folder):
    names = set()
    for sheet in _files(folder, ('.css', '.scss', '.less')):
        names.update(CSS_CLASS.findall(re.sub(r'url\([^)]*\)|\d+\.\d+', '', _read(sheet))))
    return names


def unknown_classes(folder, added):
    defined = defined_classes(folder)
    if not defined:
        return []
    added_css = set()
    for path, lines in added.items():
        if path.endswith(('.css', '.scss', '.less')):
            for line in lines:
                added_css.update(CSS_CLASS.findall(line))
    unknown = []
    for path, lines in added.items():
        if not _ui_file(path) or path.endswith(('.css', '.scss', '.less')):
            continue
        for line in lines:
            for group in USED_CLASS.findall(line):
                for name in group.split():
                    if name not in defined and name not in added_css and name not in unknown:
                        unknown.append(name)
    return unknown[:20]


def vocabulary(folder, limit=24):
    """The most used CSS classes and UI helper functions of the project (for the style brief)."""
    classes, calls, defined = Counter(), Counter(), set()
    for path in _files(folder, ('.js', '.jsx', '.ts', '.tsx', '.vue', '.svelte', '.html')):
        text = _read(path)
        for group in USED_CLASS.findall(text):
            classes.update(group.split())
        for a, b in HELPER_DEF.findall(text):
            defined.add(a or b)
        calls.update(HELPER_CALL.findall(text))
    helpers = [name for name, count in calls.most_common(200) if name in defined and count >= 5][:12]
    return [name for name, _ in classes.most_common(limit)], helpers
