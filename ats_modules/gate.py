"""Cheap deterministic checks before an agent reviews a change (the pre-review gate).

Two levels: fast checks (conflict markers, secrets in added lines, Python and JSON syntax, the
project's own linters) and targeted tests (tests of the changed files and of modules that import
them). A failed check goes back to the implementer without spending reviewer tokens.

Every result is passed, failed, skipped (nothing to check) or unavailable (the tool is missing or
could not run). A check that did not run is never reported as passed, and a passing gate never
replaces the independent review: it only decides whether the review is worth starting yet.
"""
from __future__ import annotations

import json
import re
import shlex
import shutil
from pathlib import Path

PASSED, FAILED, SKIPPED, UNAVAILABLE = 'passed', 'failed', 'skipped', 'unavailable'
FAST, TARGETED = 'fast', 'targeted'
CONFLICT = re.compile(r'^(<{7}|>{7})( |$)', re.MULTILINE)
SECRETS = (
    ('private key', re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP |ENCRYPTED )?PRIVATE KEY-----')),
    ('GitHub token', re.compile(r'\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b')),
    ('AWS access key', re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b')),
    ('API key (sk-)', re.compile(r'\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}\b')),
    ('Slack token', re.compile(r'\bxox[abposr]-[A-Za-z0-9-]{10,}\b')),
    ('Google API key', re.compile(r'\bAIza[0-9A-Za-z_-]{35}\b')),
    ('Cloudflare Access secret', re.compile(r'CF-Access-Client-Secret["\']?\s*[:=]\s*["\'][0-9a-f]{40,}["\']', re.I)),
)
TEST_FILE = re.compile(r'(^|/)(tests?/.*\.py|test_[^/]*\.py|[^/]*_test\.py)$')
OUTPUT_LIMIT = 1500


def result(name, level, status, detail=''):
    return {'name': name, 'level': level, 'status': status, 'detail': str(detail)[-OUTPUT_LIMIT:]}


def changed_paths(diff: str) -> list[str]:
    """Paths that exist after the change, from a `git diff` (deleted files are left out)."""
    paths, current = [], None
    for line in diff.splitlines():
        if line.startswith('diff --git '):
            current = None
        elif line.startswith('+++ '):
            target = line[4:].strip()
            current = None if target == '/dev/null' else target[2:] if target.startswith('b/') else target
            if current and current not in paths:
                paths.append(current)
    return paths


def added_lines(diff: str) -> dict[str, list[str]]:
    """Added lines per file."""
    found, current = {}, None
    for line in diff.splitlines():
        if line.startswith('+++ '):
            target = line[4:].strip()
            current = target[2:] if target.startswith('b/') else target
        elif line.startswith('+') and not line.startswith('+++') and current:
            found.setdefault(current, []).append(line[1:])
    return found


def conflict_markers(diff: str):
    hits = [path for path, lines in added_lines(diff).items() if CONFLICT.search('\n'.join(lines))]
    return result('conflict markers', FAST, FAILED if hits else PASSED,
                  ('Merge conflict markers in: ' + ', '.join(hits)) if hits else '')


def secrets(diff: str):
    """Names the file and kind of secret, never the value itself."""
    hits = []
    for path, lines in added_lines(diff).items():
        text = '\n'.join(lines)
        hits += [f'{path}: {name}' for name, pattern in SECRETS if pattern.search(text)]
    return result('secrets in diff', FAST, FAILED if hits else PASSED,
                  ('Possible secrets added (value not shown): ' + '; '.join(hits)) if hits else '')


def python_syntax(folder: Path, paths):
    files = [p for p in paths if p.endswith('.py') and (folder / p).is_file()]
    if not files:
        return result('python syntax', FAST, SKIPPED, 'no changed Python files')
    errors = []
    for path in files:
        try:
            compile((folder / path).read_text(encoding='utf-8', errors='replace'), path, 'exec',
                    dont_inherit=True)
        except SyntaxError as exc:
            errors.append(f'{path}:{exc.lineno}: {exc.msg}')
        except (ValueError, OSError) as exc:
            errors.append(f'{path}: {exc}')
    return result('python syntax', FAST, FAILED if errors else PASSED, '\n'.join(errors))


def json_syntax(folder: Path, paths):
    files = [p for p in paths if p.endswith('.json') and (folder / p).is_file()]
    if not files:
        return result('json syntax', FAST, SKIPPED, 'no changed JSON files')
    errors = []
    for path in files:
        text = (folder / path).read_text(encoding='utf-8', errors='replace')
        try:
            json.loads(text)
        except ValueError as exc:
            if re.search(r'^\s*//|/\*', text, re.MULTILINE):
                continue   # JSON with comments (tsconfig, VS Code settings) is not plain JSON
            errors.append(f'{path}: {exc}')
    return result('json syntax', FAST, FAILED if errors else PASSED, '\n'.join(errors))


def _argv(command):
    return shlex.split(command) if isinstance(command, str) else [str(part) for part in command]


def _run(name, level, command, folder, run, timeout, no_tests_ok=False):
    outcome = run(command, folder, timeout=timeout)
    output = (outcome.stdout or '') + (outcome.stderr or '')
    if outcome.returncode == 127 or 'cannot run test command' in output:
        return result(name, level, UNAVAILABLE, output)
    if outcome.returncode == 124 and 'timed out' in output:
        return result(name, level, UNAVAILABLE, output)
    if no_tests_ok and outcome.returncode == 5:   # pytest: no tests collected
        return result(name, level, SKIPPED, 'no tests collected')
    return result(name, level, PASSED if outcome.returncode == 0 else FAILED, output)


def linters(folder: Path, paths, conf, run, timeout):
    """The project's own `gate_commands`, plus ruff and eslint when the project configures them."""
    found = []
    for entry in conf.get('gate_commands', []) or []:
        spec = entry if isinstance(entry, dict) else {'command': entry}
        command = spec.get('command')
        if not command:
            continue
        name = str(spec.get('name') or _argv(command)[0])[:60]
        level = TARGETED if spec.get('level') == TARGETED else FAST
        found.append(_run(name, level, command, folder, run, timeout))
    if not conf.get('gate_autodetect', True):
        return found
    names = {r['name'] for r in found}
    python = [p for p in paths if p.endswith('.py') and (folder / p).is_file()]
    pyproject = folder / 'pyproject.toml'
    ruff_configured = ((folder / 'ruff.toml').is_file() or (folder / '.ruff.toml').is_file() or
                       (pyproject.is_file() and '[tool.ruff' in pyproject.read_text(encoding='utf-8', errors='replace')))
    if ruff_configured and python and 'ruff' not in names:
        ruff = shutil.which('ruff')
        found.append(_run('ruff', FAST, [ruff, 'check', *python], folder, run, timeout) if ruff else
                     result('ruff', FAST, UNAVAILABLE, 'ruff is configured but not installed'))
    scripts = [p for p in paths if p.endswith(('.js', '.jsx', '.ts', '.tsx', '.mjs')) and (folder / p).is_file()]
    eslint_configured = any((folder / name).is_file() for name in (
        '.eslintrc', '.eslintrc.json', '.eslintrc.js', '.eslintrc.cjs', 'eslint.config.js', 'eslint.config.mjs'))
    if eslint_configured and scripts and 'eslint' not in names:
        eslint = folder / 'node_modules' / '.bin' / 'eslint'
        found.append(_run('eslint', FAST, [str(eslint), *scripts], folder, run, timeout) if eslint.exists() else
                     result('eslint', FAST, UNAVAILABLE, 'eslint is configured but node_modules is missing'))
    return found


def related_tests(folder: Path, paths, tracked) -> list[str]:
    """Changed test files, tests named after a changed module, and tests that import it."""
    tests = [p for p in tracked if TEST_FILE.search(p)]
    chosen = [p for p in paths if TEST_FILE.search(p) and (folder / p).is_file()]
    stems = {Path(p).stem for p in paths if p.endswith('.py') and not TEST_FILE.search(p)}
    if stems:
        importing = re.compile(r'^\s*(?:from|import)\s+(?:[\w.]*\.)?(' + '|'.join(map(re.escape, sorted(stems)))
                               + r')\b', re.MULTILINE)
        for test in tests[:800]:
            if test in chosen:
                continue
            if Path(test).stem in {f'test_{s}' for s in stems} | {f'{s}_test' for s in stems}:
                chosen.append(test)
                continue
            try:
                if importing.search((folder / test).read_text(encoding='utf-8', errors='replace')[:20000]):
                    chosen.append(test)
            except OSError:
                continue
    return chosen[:25]


def targeted_command(conf, tests):
    """The command for the selected tests: `targeted_test_command` ({files}), else the pytest test command."""
    template = conf.get('targeted_test_command')
    if template:
        argv = _argv(template)
        if '{files}' in argv:
            index = argv.index('{files}')
            return argv[:index] + tests + argv[index + 1:]
        return argv + tests
    for command in conf.get('test_commands', []) or []:
        argv = _argv(command)
        if 'pytest' in argv:
            return argv[:argv.index('pytest') + 1] + ['-q', *tests]
    return None


def targeted_tests(folder: Path, paths, conf, tracked, run, timeout):
    tests = related_tests(folder, paths, tracked)
    if not tests:
        return result('targeted tests', TARGETED, SKIPPED, 'no tests related to the changed files')
    command = targeted_command(conf, tests)
    if command is None:
        return result('targeted tests', TARGETED, SKIPPED,
                      'no pytest test command or targeted_test_command configured')
    return _run('targeted tests', TARGETED, command, folder, run, timeout, no_tests_ok=True)


def run_gate(conf, folder, diff, tracked, run):
    """All gate results; fast checks first, targeted tests only when the fast checks pass."""
    folder = Path(folder)
    timeout = int(conf.get('gate_timeout', 300))
    paths = changed_paths(diff)
    results = [conflict_markers(diff), secrets(diff), python_syntax(folder, paths), json_syntax(folder, paths)]
    results += linters(folder, paths, conf, run, timeout)
    if failed(results):
        results.append(result('targeted tests', TARGETED, SKIPPED, 'fast checks failed first'))
    else:
        results.append(targeted_tests(folder, paths, conf, tracked, run, timeout))
    return results


def failed(results):
    return [r for r in results if r['status'] == FAILED]


def signature(results):
    """What failed, for noticing the same failure twice in a row."""
    return tuple(sorted((r['name'], r['detail'][-300:]) for r in failed(results)))


def summary(results):
    return ', '.join(f"{r['name']}: {r['status']}" for r in results)
