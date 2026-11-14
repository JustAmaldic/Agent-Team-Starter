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
import os
import re
import shlex
import shutil
from pathlib import Path, PurePosixPath

from . import style_guard, test_env

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


DEPENDENCY_FILES = re.compile(
    r'(^|/)(requirements[^/]*\.txt|pyproject\.toml|setup\.py|setup\.cfg|Pipfile|package\.json|go\.mod|Cargo\.toml|'
    r'Gemfile|composer\.json|pom\.xml|build\.gradle(\.kts)?)$')
LOCK_FILES = re.compile(r'(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Pipfile\.lock|Cargo\.lock|'
                        r'uv\.lock|go\.sum|composer\.lock|Gemfile\.lock)$')
_PY_REQ = re.compile(r'^\s*["\']?([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*([<>=!~]=?[^"\',;#]*)?')
_JSON_DEP = re.compile(r'^\s*"(@?[\w./-]+)"\s*:\s*"([~^<>=*\w./:#@+-]*)"\s*,?\s*$')
_JSON_SKIP = {'name', 'version', 'description', 'main', 'module', 'type', 'license', 'author', 'homepage',
              'repository', 'private', 'types', 'bin', 'url', 'email', 'node', 'npm'}


def dependency_changes(diff: str) -> list[dict]:
    """New or changed dependencies in this change: [{'file', 'added': [...]}].

    Read from the added lines of dependency manifests; lock files are only named. A heuristic for the
    reviewer and the PR description, never a reason to fail the gate.
    """
    found = []
    for path, lines in added_lines(diff).items():
        if LOCK_FILES.search(path):
            found.append({'file': path, 'added': [], 'lock': True})
            continue
        if not DEPENDENCY_FILES.search(path):
            continue
        names = []
        for line in lines:
            text = line.strip()
            if not text or text.startswith(('#', '//', '[', '-r ', '--')):
                continue
            if path.endswith('.json'):
                hit = _JSON_DEP.match(text)
                if hit and hit.group(1) not in _JSON_SKIP and hit.group(2)[:1] in ('~', '^', '<', '>', '=', '*') + tuple('0123456789'):
                    names.append(f'{hit.group(1)} {hit.group(2)}'.strip())
            elif path.endswith('.txt') or path.endswith('pyproject.toml') and text.startswith(('"', "'")):
                hit = _PY_REQ.match(text)
                if hit:
                    names.append((hit.group(1) + (hit.group(3) or '')).strip())
            else:
                names.append(text[:80])
        if names:
            found.append({'file': path, 'added': names[:20]})
    return found


def dependency_note(changes) -> str:
    listed = [c for c in changes or [] if c.get('added')]
    if not listed:
        return ''
    return ('NEW OR CHANGED DEPENDENCIES in this change: '
            + '; '.join(f"{c['file']}: {', '.join(c['added'][:10])}" for c in listed[:6])
            + '. Check that each one is really needed for the task, is a known maintained package under that exact '
            'name (typosquatting) and is pinned the way the project pins its other dependencies. An unneeded or '
            'unknown dependency is a MAJOR finding. ')


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
    """A command string as arguments. On Windows backslashes are path separators, not escapes."""
    if not isinstance(command, str):
        return [str(part) for part in command]
    if os.name == 'nt':
        return [part[1:-1] if len(part) > 1 and part[0] == part[-1] == '"' else part
                for part in shlex.split(command, posix=False)]
    return shlex.split(command)


def _run(name, level, command, folder, run, timeout, no_tests_ok=False):
    outcome = run(command, folder, timeout=timeout)
    output = (outcome.stdout or '') + (outcome.stderr or '')
    if outcome.returncode == 127 or 'cannot run test command' in output:
        return result(name, level, UNAVAILABLE, output)
    if outcome.returncode == 124 and 'timed out' in output:
        return result(name, level, UNAVAILABLE, output)
    module = test_env.missing_module(output, folder) if outcome.returncode else None
    if module:   # the test environment lacks a package: no code change can fix that
        found = result(name, level, UNAVAILABLE, output)
        found['environment'] = module
        return found
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


MAX_UNITTEST_FILES = 5


def unittest_commands(conf, tests):
    """`python -m unittest discover -s <dir>` test commands, narrowed to one test file each (-p).

    unittest has no way to select several files of a discover run at once, so this is one command
    per file, for the files inside the start directory, at most MAX_UNITTEST_FILES.
    """
    for command in conf.get('test_commands', []) or []:
        argv = _argv(command)
        if 'unittest' not in argv or 'discover' not in argv:
            continue
        head = argv[:argv.index('discover') + 1]
        rest = argv[len(head):]
        options, start, top = [], '.', None
        index = 0
        while index < len(rest):
            flag = rest[index]
            value = rest[index + 1] if index + 1 < len(rest) else None
            if flag in ('-s', '--start-directory') and value is not None:
                start, index = value, index + 2
            elif flag in ('-t', '--top-level-directory') and value is not None:
                top, index = value, index + 2
            elif flag in ('-p', '--pattern') and value is not None:
                index += 2   # replaced by the file below
            else:
                options.append(flag)
                index += 1
        base = PurePosixPath(start.replace('\\', '/').rstrip('/') or '.')
        commands = []
        for test in tests:
            test_path = PurePosixPath(test)
            inside = str(base) == '.' or str(test_path).startswith(str(base) + '/')
            if inside and test_path.suffix == '.py':
                commands.append(head + ['-s', start, *(['-t', top] if top else []), '-p', test_path.name, *options])
        return commands[:MAX_UNITTEST_FILES]
    return []


def targeted_tests(folder: Path, paths, conf, tracked, run, timeout):
    tests = related_tests(folder, paths, tracked)
    if not tests:
        return result('targeted tests', TARGETED, SKIPPED, 'no tests related to the changed files')
    command = targeted_command(conf, tests)
    if command is not None:
        return _run('targeted tests', TARGETED, command, folder, run, timeout, no_tests_ok=True)
    commands = unittest_commands(conf, tests)
    if not commands:
        return result('targeted tests', TARGETED, SKIPPED,
                      'no pytest or unittest test command, or targeted_test_command, configured')
    outcomes = [_run('targeted tests', TARGETED, c, folder, run, timeout, no_tests_ok=True) for c in commands]
    environment = [o for o in outcomes if o.get('environment')]
    if environment:   # a missing package explains the other failures too
        found = result('targeted tests', TARGETED, UNAVAILABLE, '\n'.join(o['detail'] for o in environment))
        found['environment'] = environment[0]['environment']
        return found
    for status in (FAILED, UNAVAILABLE):
        bad = [o for o in outcomes if o['status'] == status]
        if bad:
            return result('targeted tests', TARGETED, status, '\n'.join(o['detail'] for o in bad))
    ran = [o for o in outcomes if o['status'] == PASSED]
    if ran:
        return result('targeted tests', TARGETED, PASSED, f'{len(ran)} test files')
    return result('targeted tests', TARGETED, SKIPPED, 'no tests ran')


def run_gate(conf, folder, diff, tracked, run):
    """All gate results; fast checks first, targeted tests only when the fast checks pass."""
    folder = Path(folder)
    timeout = int(conf.get('gate_timeout', 300))
    paths = changed_paths(diff)
    results = [conflict_markers(diff), secrets(diff), python_syntax(folder, paths), json_syntax(folder, paths)]
    results.append(style_guard.check(folder, diff, conf))   # off with style_check: false
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
