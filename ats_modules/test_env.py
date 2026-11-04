"""The Python environment the project's tests run in.

ATS itself may run on another Python than the project's tests need. A test command such as
`python -m unittest` finds `python` through PATH, so without help it uses whatever interpreter the
computer has first, which often lacks the project's dependencies (for example `fastapi`). Then a
test fails with `ModuleNotFoundError` and an agent is asked to "repair" something no code change
can fix: agents never install anything outside the worktree.

This module:

- picks the project's virtualenv (`python_env` in .ats-project.json, or `.venv` / `venv` in the
  project folder) and puts its scripts folder first in PATH for test commands and agent CLIs;
- checks before any agent starts that the project's requirements are installed in the Python
  the tests will use;
- recognises a missing third-party module in test output, so ATS reports a configuration
  problem instead of starting a repair.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

AUTO_ENVS = ('.venv', 'venv')
REQUIREMENT_FILES = ('requirements.txt', 'requirements-dev.txt', 'requirements-test.txt', 'requirements_dev.txt')
MISSING_MODULE = re.compile(r"(?:ModuleNotFoundError|ImportError): No module named '?([A-Za-z_][\w.]*)'?")
PYTHON_NAME = re.compile(r'^python(\d+(\.\d+)?)?(\.exe)?$', re.IGNORECASE)
REQUIREMENT_NAME = re.compile(r'^([A-Za-z0-9][A-Za-z0-9._-]*)')


def scripts_dir(env: Path) -> Path:
    return env / ('Scripts' if os.name == 'nt' else 'bin')


def python_in(env: Path) -> Path:
    return scripts_dir(env) / ('python.exe' if os.name == 'nt' else 'python')


def resolve(conf: dict, root: Path) -> dict:
    """{'env': folder or None, 'source': 'configured' | 'auto' | None, 'error': text or None}.

    `python_env` may be relative to the project folder (".venv", shared through git) or absolute.
    """
    root = Path(root)
    configured = conf.get('python_env')
    if configured:
        env = Path(os.path.expanduser(str(configured)))
        env = env if env.is_absolute() else root / env
        if not python_in(env).is_file():
            return {'env': None, 'source': 'configured',
                    'error': f'python_env {configured} has no {python_in(Path(str(configured)))}'}
        return {'env': env, 'source': 'configured', 'error': None}
    for name in AUTO_ENVS:
        if python_in(root / name).is_file():
            return {'env': root / name, 'source': 'auto', 'error': None}
    return {'env': None, 'source': None, 'error': None}


def environ(base: dict, env: Path | None) -> dict:
    """`base` with the virtualenv's scripts first in PATH (as `activate` does)."""
    if env is None:
        return base
    result = dict(base)
    result['PATH'] = str(scripts_dir(env)) + os.pathsep + result.get('PATH', '')
    result['VIRTUAL_ENV'] = str(env)
    result.pop('PYTHONHOME', None)
    return result


def _argv(command):
    if isinstance(command, list):
        return [str(part) for part in command]
    try:
        return shlex.split(str(command), posix=os.name != 'nt')
    except ValueError:
        return str(command).split()


def python_of(command) -> str | None:
    """The Python a test command starts (`python -m pytest`, `python3 -m unittest`), else None."""
    argv = _argv(command)
    if not argv:
        return None
    name = Path(argv[0].strip('"')).name
    return argv[0].strip('"') if PYTHON_NAME.match(name) or name.lower() in ('py', 'py.exe') else None


def requirements(root: Path) -> list[str]:
    """Distribution names from the project's requirement files (no markers, URLs or -r lines)."""
    names = []
    for filename in REQUIREMENT_FILES:
        path = Path(root) / filename
        try:
            lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.split('#', 1)[0].strip()
            if not line or line.startswith('-') or ';' in line or '://' in line or line.startswith('.'):
                continue
            match = REQUIREMENT_NAME.match(line)
            if match and match.group(1) not in names:
                names.append(match.group(1))
    return names


CHECK_CODE = (
    'import sys, json\n'
    'from importlib import metadata\n'
    'missing = []\n'
    'for name in sys.argv[1:]:\n'
    '    try:\n'
    '        metadata.distribution(name)\n'
    '    except metadata.PackageNotFoundError:\n'
    '        missing.append(name)\n'
    'print(json.dumps({"python": sys.executable, "missing": missing}))\n'
)


def check(conf: dict, root: Path, base_env: dict, run=subprocess.run) -> dict:
    """Are the project's requirements installed in the Python its test commands use?

    Returns {'ok', 'python', 'env', 'source', 'missing', 'message'}. Only Python test commands
    are checked; anything else (npm, make, pytest without python) reports ok without checking.
    """
    resolved = resolve(conf, root)
    report = {'ok': True, 'python': None, 'env': str(resolved['env']) if resolved['env'] else None,
              'source': resolved['source'], 'missing': [], 'message': ''}
    if resolved['error']:
        report.update(ok=False, message=resolved['error'])
        return report
    names = requirements(root)
    pythons = [p for p in (python_of(c) for c in conf.get('test_commands') or []) if p]
    if not names or not pythons:
        return report
    env = environ(base_env, resolved['env'])
    for python in dict.fromkeys(pythons):
        try:
            outcome = run([python, '-c', CHECK_CODE, *names], cwd=str(root), env=env, capture_output=True,
                          text=True, timeout=60, shell=False)
        except (OSError, subprocess.SubprocessError) as exc:
            report.update(ok=False, python=python, message=f'{python} cannot start: {exc}')
            return report
        try:
            data = json.loads((outcome.stdout or '').strip().splitlines()[-1])
        except (ValueError, IndexError):
            report.update(ok=False, python=python,
                          message=f'{python} cannot check packages: {(outcome.stderr or outcome.stdout or "")[-300:]}')
            return report
        report['python'] = data.get('python') or python
        if data.get('missing'):
            report.update(ok=False, missing=data['missing'])
            report['message'] = missing_message(report)
            return report
    return report


def missing_message(report: dict) -> str:
    python = report.get('python') or 'python'
    where = (f"the virtualenv {report['env']}" if report.get('env')
             else 'found through PATH; the project has no .venv, venv or python_env')
    return (f"Test environment incomplete: {', '.join(report['missing'])} not installed in {python} "
            f"({where}). Install the project's requirements there, for example: "
            f"python3 -m venv .venv && .venv/bin/pip install -r requirements.txt, or set python_env in "
            f".ats-project.json. Agents never install packages; no agent was started for this.")


def missing_module(output: str, folder: Path | None = None) -> str | None:
    """The third-party module a test run could not import, or None.

    A module that exists in the worktree (a project file the change broke) is a code problem
    and returns None, so the implementer still gets to fix it.
    """
    match = MISSING_MODULE.search(output or '')
    if not match:
        return None
    top = match.group(1).split('.')[0]
    if folder is not None:
        folder = Path(folder)
        for base in (folder, folder / 'src'):
            if (base / top).is_dir() or (base / f'{top}.py').is_file():
                return None
    return top


def absolute(command, env: Path | None):
    """The test command with `python`/`python3` replaced by the virtualenv's interpreter."""
    if env is None:
        return command
    argv = _argv(command)
    if not argv or not python_of(command) or Path(argv[0]).is_absolute():
        return command
    return ' '.join(shlex.quote(part) for part in [str(python_in(env)), *argv[1:]])


def agent_instructions(conf: dict, env: Path | None) -> str:
    """How agents run tests: the exact interpreter, targeted tests only, the suite is ATS's job."""
    commands = [absolute(c, env) if isinstance(c, str) else ' '.join(map(str, c))
                for c in conf.get('test_commands') or []]
    if not commands:
        return ''
    python = str(python_in(env)) if env is not None else None
    lines = ['TESTS: ATS runs the full test commands itself after you and gives the results to the reviewer: '
             + '; '.join(commands[:4]) + '. Do not run the whole suite yourself; run only the tests for what you '
             'changed.']
    if python:
        lines.append(f'Use exactly this Python for every Python command: {python} (for example '
                     f'{python} -m pytest -q tests/test_x.py). A login shell (zsh -l, bash -l) can put another '
                     'Python first in PATH; that one lacks the project packages.')
    return '\n' + ' '.join(lines)
