"""Compact, cached context so agents spend fewer tokens finding their way around a repository.

Two layers:
1. Repository context: a map of the directories, the key files (README, build and test config),
   entry points and the test layout.
2. Part context: the part's files with a short outline (classes, functions, exports), the tests
   that belong to them and the local modules they import.

Both are keyed by the content of what they describe (the blob ids in the worktree's tree hash,
see ats.index_tree), not by a commit, so a package always matches the code as it is. Packages are
immutable files in a cache directory: parallel agents working from the same tree share them, and a
change to a relevant file gives a new key. The package is orientation only; agents may and should
read further themselves.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections import Counter
from pathlib import Path, PurePosixPath

KEY_FILES = ('README.md', 'README.rst', 'README', 'AGENTS.md', 'CLAUDE.md', 'pyproject.toml', 'setup.cfg',
             'package.json', 'requirements.txt', 'Makefile', 'tsconfig.json', 'go.mod', 'Cargo.toml',
             '.ats-project.json')
ENTRY_NAMES = {'main.py', '__main__.py', 'app.py', 'server.py', 'cli.py', 'manage.py', 'index.js', 'main.js',
               'index.ts', 'main.ts', 'main.go', 'main.rs'}
SKIP_PARTS = {'node_modules', '.git', 'dist', 'build', '.venv', 'venv', '__pycache__', 'vendor', '.next',
              'coverage', 'site-packages'}
OUTLINE = {
    '.py': re.compile(r'^(?: {0,4})(?:async def|def|class) [A-Za-z_]\w*.*$', re.MULTILINE),
    '.js': re.compile(r'^(?:export\s+)?(?:async\s+)?(?:function\*?|class|const|let)\s+[A-Za-z_$][\w$]*.*$', re.MULTILINE),
}
for _suffix in ('.jsx', '.ts', '.tsx', '.mjs'):
    OUTLINE[_suffix] = OUTLINE['.js']
REPO_LIMIT = 2500
PART_LIMIT = 5000
CACHE_KEEP = 300


def _git(args, folder):
    result = subprocess.run(['git', *args], cwd=folder, capture_output=True, text=True, encoding='utf-8',
                            errors='replace', check=False)
    return result.stdout if result.returncode == 0 else ''


def tree_entries(folder, tree) -> dict[str, str]:
    """{path: blob id} for every file in the tree."""
    entries = {}
    for line in _git(['ls-tree', '-r', '--full-tree', tree], folder).splitlines():
        meta, _, path = line.partition('\t')
        parts = meta.split()
        if len(parts) == 3 and parts[1] == 'blob':
            entries[path] = parts[2]
    return entries


def _key(*parts) -> str:
    return hashlib.sha256('\0'.join(map(str, parts)).encode('utf-8')).hexdigest()[:24]


def _cached(cache: Path, name: str, build):
    path = cache / name
    try:
        return path.read_text(encoding='utf-8')
    except OSError:
        pass
    text = build()
    try:
        cache.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f'.{os.getpid()}.tmp')
        temporary.write_text(text, encoding='utf-8')
        os.replace(temporary, path)   # atomic: a parallel agent reads either nothing or the whole file
        packages = sorted(cache.glob('*.txt'), key=lambda p: p.stat().st_mtime)
        for old in packages[:-CACHE_KEEP]:   # keep the cache small; old trees are rarely needed again
            old.unlink(missing_ok=True)
    except OSError:
        pass
    return text


def _visible(path: str) -> bool:
    return not (set(PurePosixPath(path).parts) & SKIP_PARTS)


def repository_layer(folder, tree, cache: Path, limit=REPO_LIMIT) -> str:
    entries = {p: b for p, b in tree_entries(folder, tree).items() if _visible(p)}
    key = _key('repo', '\n'.join(sorted(entries)), *(entries.get(name, '') for name in KEY_FILES))

    def build():
        directories = Counter()
        suffixes = Counter()
        for path in entries:
            pure = PurePosixPath(path)
            directories['/'.join(pure.parts[:-1][:2]) or '.'] += 1
            suffixes[pure.suffix or pure.name] += 1
        lines = ['Repository map (orientation only; read the files themselves before changing them):',
                 f'- {len(entries)} files; mostly ' + ', '.join(f'{s} ({n})' for s, n in suffixes.most_common(5))]
        lines.append('- Folders (files): ' + ', '.join(f'{d}/ ({n})' for d, n in sorted(directories.items())[:40]))
        keys = [name for name in KEY_FILES if name in entries]
        if keys:
            lines.append('- Key files: ' + ', '.join(keys))
        entry_points = sorted(p for p in entries if PurePosixPath(p).name in ENTRY_NAMES and p.count('/') <= 2)
        if entry_points:
            lines.append('- Entry points: ' + ', '.join(entry_points[:10]))
        tests = sorted({str(PurePosixPath(p).parent) for p in entries
                        if re.search(r'(^|/)(test_[^/]*|[^/]*_test)\.\w+$|(^|/)tests?/', p)})
        if tests:
            lines.append('- Tests live in: ' + ', '.join(tests[:10]))
        text = '\n'.join(lines)
        return text[:limit]

    return _cached(cache, f'repo-{key}.txt', build)


def _owned_files(entries, owned_paths):
    files = []
    for owned in owned_paths:
        owned = owned.strip().rstrip('/')
        matches = [p for p in entries if p == owned or p.startswith(owned + '/')]
        files += [p for p in sorted(matches) if p not in files][:15]
    return files


def _outline(path, text):
    pattern = OUTLINE.get(PurePosixPath(path).suffix)
    if pattern is None:
        return [line for line in text.splitlines() if line.strip()][:3]
    return [line.strip()[:140] for line in pattern.findall(text)][:30]


def _local_imports(text, entries):
    names = []
    for module, imported in re.findall(r'^\s*from\s+([\w.]+)\s+import\s+([\w, ]+)', text, re.MULTILINE):
        names.append(module)
        names += [module + '.' + part.strip().split(' ')[0] for part in imported.split(',') if part.strip()]
    names += re.findall(r'^\s*import\s+([\w.]+)', text, re.MULTILINE)
    found = []
    for name in names:
        base = name.lstrip('.').replace('.', '/')
        for candidate in (base + '.py', base + '/__init__.py'):
            if base and candidate in entries and candidate not in found:
                found.append(candidate)
    return found


def part_layer(folder, tree, cache: Path, part: str, owned_paths=(), limit=PART_LIMIT) -> str:
    """The part's own files, related tests and imports. Empty when the part names no known files."""
    folder = Path(folder)
    entries = tree_entries(folder, tree)
    files = _owned_files(entries, owned_paths)
    if not files:
        files = [p for p in entries if _visible(p) and len(p) > 4 and p in part][:10]
    if not files:
        return ''
    stems = {PurePosixPath(p).stem for p in files}
    tests = [p for p in entries if _visible(p) and PurePosixPath(p).stem in
             {f'test_{s}' for s in stems} | {f'{s}_test' for s in stems} and p not in files][:10]
    key = _key('part', part, *(f'{p}:{entries.get(p, "new")}' for p in files + tests))

    def build():
        lines = ['Files for this part (outline only; open them before editing):']
        imports = []
        for path in files:
            try:
                text = (folder / path).read_text(encoding='utf-8', errors='replace')[:200000]
            except OSError:
                lines.append(f'- {path} (new file)')
                continue
            lines.append(f'- {path} ({text.count(chr(10)) + 1} lines)')
            lines += ['    ' + line for line in _outline(path, text)]
            imports += [p for p in _local_imports(text, entries) if p not in files and p not in imports]
        if tests:
            lines.append('Related tests: ' + ', '.join(tests))
        if imports:
            lines.append('Local modules these files import: ' + ', '.join(imports[:12]))
        text = '\n'.join(lines)
        return text if len(text) <= limit else text[:limit].rsplit('\n', 1)[0] + '\n    …'

    return _cached(cache, f'part-{key}.txt', build)
