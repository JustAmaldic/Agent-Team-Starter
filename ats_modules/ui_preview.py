"""Screenshots of the screens a change touches, for the reviewer.

A static style check cannot show what a screen looks like in a browser. When a change touches UI
files and the screen map knows their screen, ATS starts the app on a free local port in a
throw-away checkout, opens the screen with Playwright at desktop and phone width, and gives the
reviewer the images: "before" (the base), "after" (this change) and "reference" (the existing
screen the part should match). Browser console errors on those pages go along as text.

Configured per project in .ats-project.json:

    "ui_preview": {"command": "python3 server.py --port {port}", "url": "http://127.0.0.1:{port}",
                   "ready_path": "/", "timeout": 40, "widths": [1280, 390], "before": true}

Everything runs locally and only on 127.0.0.1. A preview that cannot start never blocks a run: the
reviewer gets a note why there are no screenshots. ATS never installs Playwright itself.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

from . import screen_map

UI_SUFFIXES = {'.css', '.scss', '.less', '.js', '.jsx', '.mjs', '.ts', '.tsx', '.html', '.vue', '.svelte'}
DEFAULT_WIDTHS = (1280, 390)
MAX_SCREENS = 3
SHOT_SCRIPT = r'''
import json, sys
from playwright.sync_api import sync_playwright
jobs = json.loads(sys.argv[1])
results = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    for job in jobs:
        page = browser.new_page(viewport={'width': job['width'], 'height': 900 if job['width'] > 600 else 844},
                                device_scale_factor=1)
        errors = []
        page.on('console', lambda m, errors=errors: errors.append(m.text[:300]) if m.type == 'error' else None)
        page.on('pageerror', lambda e, errors=errors: errors.append(str(e)[:300]))
        item = dict(job)
        try:
            page.goto(job['url'], wait_until='networkidle', timeout=job['timeout_ms'])
            page.wait_for_timeout(job['wait_ms'])
            page.screenshot(path=job['path'], full_page=True)
        except Exception as exc:
            item['error'] = str(exc)[:300]
        item['console_errors'] = errors[:10]
        results.append(item)
        page.close()
    browser.close()
print('ATS_SHOTS_JSON: ' + json.dumps(results))
'''


def settings(conf) -> dict | None:
    value = (conf or {}).get('ui_preview')
    if not isinstance(value, dict) or not value.get('enabled', True):
        return None
    if not isinstance(value.get('command'), str) or not value['command'].strip():
        return None
    widths = [int(w) for w in value.get('widths') or DEFAULT_WIDTHS if str(w).isdigit() and 200 <= int(w) <= 3000]
    return {'command': value['command'].strip(),
            'url': str(value.get('url') or 'http://127.0.0.1:{port}').strip(),
            'ready_path': str(value.get('ready_path') or '/'),
            'timeout': max(5, min(int(value.get('timeout', 40)), 180)),
            'widths': widths[:3] or list(DEFAULT_WIDTHS),
            'before': bool(value.get('before', True)),
            'wait_ms': max(0, min(int(value.get('wait_ms', 800)), 10000)),
            'env': {str(k): str(v) for k, v in (value.get('env') or {}).items()
                    if isinstance(k, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,60}', k)}}


def touches_ui(paths) -> bool:
    return any(PurePosixPath(p).suffix in UI_SUFFIXES for p in paths or ())


def plan(data, changed, reference=None) -> tuple[list[dict], list[str]]:
    """Which screens to capture: [{'role', 'screen', 'path'}] and notes about screens that cannot be."""
    notes, jobs = [], []
    changed_screens = screen_map.for_files(data, [p for p in changed if PurePosixPath(p).suffix in UI_SUFFIXES])
    if not data:
        return [], [f'No screenshots: the project has no {screen_map.FILE}, so ATS does not know which screen '
                    'these files belong to.']
    if not changed_screens:
        return [], [f'No screenshots: no screen in {screen_map.FILE} lists the changed UI files.']
    seen = set()
    for role, screens in (('after', changed_screens), ('reference', screen_map.for_files(data, [reference] if reference else []))):
        for screen in screens:
            target = screen.get('preview') or screen.get('route')
            if not target:
                notes.append(f"No screenshot of {screen['name']}: it has no route or preview path in {screen_map.FILE}.")
                continue
            if re.search(r'[:{\[<]', target.split('#', 1)[0]) and not screen.get('preview'):
                notes.append(f"No screenshot of {screen['name']}: its route {target} has a parameter; add a "
                             f"preview path with an example value to {screen_map.FILE}.")
                continue
            if (role, screen['name']) in seen or len([j for j in jobs if j['role'] == role]) >= MAX_SCREENS:
                continue
            seen.add((role, screen['name']))
            jobs.append({'role': role, 'screen': screen['name'], 'path': target})
    return jobs, notes


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def page_url(base: str, path: str) -> str:
    if re.match(r'^https?://', path):
        return path if path.startswith(('http://127.0.0.1', 'http://localhost')) else base.rstrip('/') + '/'
    if path.startswith('#'):
        return base.rstrip('/') + '/' + path
    return base.rstrip('/') + '/' + path.lstrip('/')


def _argv(command: str):
    return command if os.name == 'nt' else shlex.split(command)


def start(command, folder, env, log):
    kwargs = {'start_new_session': True} if os.name != 'nt' else {
        'creationflags': getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)}
    return subprocess.Popen(_argv(command), cwd=str(folder), env=env, stdout=log, stderr=subprocess.STDOUT,
                            shell=os.name == 'nt', **kwargs)


def stop(process):
    if process.poll() is not None:
        return
    try:
        if os.name != 'nt':
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name != 'nt':
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            pass


def wait_ready(url, process, timeout) -> str | None:
    """None when the app answers; otherwise why not."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return f'the preview command stopped with exit code {process.returncode}'
        try:
            with urllib.request.urlopen(url, timeout=3) as response:   # noqa: S310 (local only)
                if response.status < 500:
                    return None
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return None
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.5)
    return f'the app did not answer on {url} within {timeout}s'


def playwright_available(python=sys.executable) -> bool:
    try:
        return subprocess.run([python, '-c', 'import playwright.sync_api'], capture_output=True,
                              timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def capture(folder, preview, jobs, out_dir, label, env, python=sys.executable) -> tuple[list[dict], str | None]:
    """Start the app in `folder`, take the screenshots, stop the app. Returns (shots, problem)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    port = free_port()
    base = preview['url'].replace('{port}', str(port))
    if not re.match(r'^http://(127\.0\.0\.1|localhost)(:\d+)?(/|$)', base):
        return [], 'ui_preview.url must be a local http://127.0.0.1 or http://localhost address'
    run_env = {**env, **preview['env'], 'PORT': str(port)}
    with (out_dir / f'{label}-app.log').open('w', encoding='utf-8', errors='replace') as log:
        process = start(preview['command'].replace('{port}', str(port)), folder, run_env, log)
        try:
            problem = wait_ready(page_url(base, preview['ready_path']), process, preview['timeout'])
            if problem:
                return [], problem
            work = []
            for job in jobs:
                for width in preview['widths']:
                    name = re.sub(r'[^a-z0-9]+', '-', job['screen'].lower()).strip('-') or 'screen'
                    work.append({**job, 'label': label, 'width': width, 'url': page_url(base, job['path']),
                                 'path_on_disk': str(out_dir / f'{label}-{name}-{width}.png'),
                                 'timeout_ms': preview['timeout'] * 1000, 'wait_ms': preview['wait_ms']})
            payload = [dict(j, path=j['path_on_disk']) for j in work]
            try:
                outcome = subprocess.run([python, '-c', SHOT_SCRIPT, json.dumps(payload)], capture_output=True,
                                         text=True, timeout=60 + 30 * len(payload), env=env)
            except subprocess.TimeoutExpired:
                return [], 'taking the screenshots timed out'
            line = next((l for l in reversed(outcome.stdout.splitlines()) if l.startswith('ATS_SHOTS_JSON: ')), '')
            if not line:
                return [], 'Playwright failed: ' + (outcome.stderr or outcome.stdout)[-300:]
            shots = []
            for item, job in zip(json.loads(line[len('ATS_SHOTS_JSON: '):]), work):
                shot = {'role': job['role'], 'screen': job['screen'], 'width': job['width'],
                        'route': job['path'], 'file': Path(job['path_on_disk']).name,
                        'console_errors': item.get('console_errors', [])}
                if item.get('error') or not Path(job['path_on_disk']).is_file():
                    shot['error'] = item.get('error') or 'no image written'
                shots.append(shot)
            return shots, None
        finally:
            stop(process)


def reviewer_note(result, directory) -> str:
    """The text the reviewer gets."""
    if not result:
        return ''
    lines = []
    shots = [s for s in result.get('shots', []) if not s.get('error')]
    if shots:
        lines.append('SCREENSHOTS of the changed UI (open these PNG files and look at them; "before" is the base, '
                     '"after" this change, "reference" the existing screen to match). Visibly inconsistent or broken '
                     'UI is a MAJOR finding; small visual deviations from the reference are MINOR:')
        for shot in shots:
            lines.append(f"- {shot['role']} · {shot['screen']} · {shot['width']}px: {Path(directory) / shot['file']}")
        errors = sorted({e for s in result['shots'] if s['role'] == 'after' for e in s.get('console_errors', [])})
        if errors:
            lines.append('Browser console errors after the change: ' + ' | '.join(errors[:8]))
    for note in result.get('notes', []):
        lines.append(note)
    return '\n'.join(lines)
