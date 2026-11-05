"""CSS classes used in a change that no stylesheet of the project defines: a note for the reviewer.

Not a failure (a class can be a JavaScript hook). The style gate itself, the design brief and the
reference screen are in style_guard.py.
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
