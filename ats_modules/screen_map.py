"""The screen map: which files, functions and routes belong to which screen of the project.

A task such as "Verander de knop op de device detail pagina" names a screen in the user's words.
Without help an agent searches the whole repository for it, and sometimes changes the wrong
screen. Comments in the code do not solve this: a search only finds them when the words match
exactly, and they go stale when code moves.

`.ats-screens.json` in the project root lists each screen once:

    {"version": 1, "screens": [
      {"name": "Device detail", "aliases": ["apparaatpagina", "device pagina"],
       "route": "#device", "preview": "#device", "files": ["static/js/apparaat.js"],
       "symbols": ["renderDevice"], "api": ["GET /api/devices/{id}"], "tests": ["tests/test_devices.py"]}]}

- `generate` makes a first version from the code (render functions, view sections, file-based
  and router-config routes, HTML page handlers) and keeps everything a person already wrote;
- `match` finds the screens a task names (name, aliases, route or function, case-insensitive,
  also inside Dutch compounds such as "instellingenpagina");
- `context` is the prompt text: the matched screens with their files, or the list of screens when
  the task talks about a screen without naming a known one;
- `uncovered` finds screens a change adds that the map does not list, for the reviewer.

The map is orientation only. Agents still read the code; ATS never trusts the map for safety.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path, PurePosixPath

FILE = '.ats-screens.json'
SOURCE_SUFFIXES = {'.py', '.js', '.jsx', '.mjs', '.ts', '.tsx', '.vue', '.svelte', '.html'}
SKIP = {'node_modules', '.git', 'dist', 'build', '.venv', 'venv', 'vendor', '__pycache__', 'coverage',
        'site-packages', '.next', '.svelte-kit', 'tests', 'test', '__tests__'}
MAX_FILES = 2000
MAX_BYTES = 400000
MAX_SCREENS = 200
LIST_FIELDS = ('aliases', 'files', 'symbols', 'api', 'tests')
TEXT_FIELDS = ('route', 'preview', 'notes')
SCREEN_WORDS = re.compile(r'\b(pagina|scherm|page|screen|view|tab|venster|dialoog|dialog|menu|knop|button|'
                          r'formulier|form|layout|styling|stijl|ui)\w*', re.IGNORECASE)

# render<Name> functions, and show<Name>Page/Screen/View (show<Name> alone usually toggles a detail).
RENDER_FUNCTION = re.compile(
    r'^[ \t]*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(render|show)([A-Z]\w*)\s*\(|'
    r'^[ \t]*(?:export\s+)?(?:const|let)\s+(render|show)([A-Z]\w*)\s*=\s*(?:async\s*)?(?:\(|function)',
    re.MULTILINE)
SCREEN_SUFFIX = re.compile(r'(Page|Screen|View)$')
COMPONENT = re.compile(
    r'^[ \t]*(?:export\s+)?(?:default\s+)?(?:function|class|const)\s+([A-Z]\w*?)(Page|Screen|View)\b', re.MULTILINE)
VIEW_SECTION = re.compile(r'<(?:section|div|main)\b[^>]*\bid=["\'](?:view|page|screen)-([\w-]+)["\']', re.IGNORECASE)
VIEW_LABEL = re.compile(r'data-(?:view|page|screen)=["\']([\w-]+)["\'][^>]*>((?:(?!</).){1,400}?)</', re.IGNORECASE | re.DOTALL)
ROUTER_ENTRY = re.compile(r'path\s*:\s*["\'](/[^"\']*)["\']\s*,[^{}]{0,200}?(?:component|element)\s*:\s*<?\s*([A-Z]\w*)',
                          re.DOTALL)
JSX_ROUTE = re.compile(r'<Route\b[^>]*\bpath=["\'](/[^"\']*)["\'][^>]*?(?:element=\{<\s*|component=\{\s*)([A-Z]\w*)')
PY_ROUTE = re.compile(r'^[ \t]*@\w+(?:\.\w+)*\.(?:get|route)\(\s*["\'](/[^"\']*)["\'][^\n]*\n'
                      r'(?:[ \t]*@[^\n]*\n)*[ \t]*(?:async\s+)?def\s+(\w+)', re.MULTILINE)
PY_HTML = re.compile(r'TemplateResponse|render_template|HTMLResponse|\.html["\']')


def _words(name: str) -> str:
    """'DeviceDetail' or 'device-detail' -> 'Device detail'."""
    text = re.sub(r'[_-]+', ' ', name)
    text = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', text).strip().lower()
    return text[:1].upper() + text[1:]


def _norm(text: str) -> str:
    return ' ' + ' '.join(re.sub(r'[^0-9a-zà-ÿ/#:{}.]+', ' ', str(text).lower()).split()) + ' '


def path_of(folder) -> Path:
    return Path(folder) / FILE


def _clean(screen) -> dict | None:
    if not isinstance(screen, dict) or not isinstance(screen.get('name'), str) or not screen['name'].strip():
        return None
    result = {'name': screen['name'].strip()[:120]}
    for field in LIST_FIELDS:
        values = screen.get(field) or []
        if isinstance(values, str):
            values = [values]
        if isinstance(values, list):
            kept = [str(v).strip()[:200] for v in values if isinstance(v, (str, int)) and str(v).strip()]
            if kept:
                result[field] = list(dict.fromkeys(kept))[:30]
    for field in TEXT_FIELDS:
        if isinstance(screen.get(field), str) and screen[field].strip():
            result[field] = screen[field].strip()[:300]
    if screen.get('generated') is True:
        result['generated'] = True
    return result


def load(folder) -> tuple[dict | None, str | None]:
    """(map, problem). No file: (None, None). An unreadable file: (None, reason)."""
    path = path_of(folder)
    if not path.is_file():
        return None, None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        return None, f'{FILE} is not valid JSON: {exc}'
    if not isinstance(data, dict) or not isinstance(data.get('screens'), list):
        return None, f'{FILE} needs an object with a "screens" list'
    screens = [s for s in (_clean(s) for s in data['screens'][:MAX_SCREENS]) if s]
    return {'version': 1, 'screens': screens}, None


def source_files(folder) -> list[str]:
    root = Path(folder)
    found = []
    for parent, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith('.')
                         and not (Path(parent) / d).is_symlink())
        for name in sorted(files):
            path = Path(parent) / name
            if path.suffix in SOURCE_SUFFIXES and not path.is_symlink() and not name.endswith('.min.js'):
                found.append(path.relative_to(root).as_posix())
                if len(found) >= MAX_FILES:
                    return found
    return found


def _read(folder, path) -> str:
    try:
        with (Path(folder) / path).open(encoding='utf-8', errors='replace') as stream:
            return stream.read(MAX_BYTES)
    except OSError:
        return ''


def _file_route(path: str) -> str | None:
    """Routes of file-based routers (Next.js pages/ and app/, SvelteKit src/routes)."""
    pure = PurePosixPath(path)
    parts = list(pure.parts)
    if 'pages' in parts and pure.suffix in {'.js', '.jsx', '.ts', '.tsx'}:
        rest = parts[parts.index('pages') + 1:]
        if not rest or rest[0] == 'api' or rest[-1].startswith('_'):
            return None
        rest[-1] = PurePosixPath(rest[-1]).stem
        if rest[-1] == 'index':
            rest = rest[:-1]
    elif 'app' in parts and pure.stem == 'page' and pure.suffix in {'.js', '.jsx', '.ts', '.tsx'}:
        rest = [p for p in parts[parts.index('app') + 1:-1] if not p.startswith('(')]
    elif 'routes' in parts and pure.name == '+page.svelte':
        rest = [p for p in parts[parts.index('routes') + 1:-1] if not p.startswith('(')]
    else:
        return None
    rest = [':' + p.strip('[]').lstrip('.') if p.startswith('[') else p for p in rest]
    return '/' + '/'.join(rest)


def detect_text(path: str, text: str) -> list[dict]:
    """Screen candidates in one file: {'name', 'route', 'files', 'symbols', 'line'}."""
    found = []

    def add(name, line, route=None, symbol=None, aliases=()):
        if not name:
            return
        item = {'name': name, 'files': [path], 'line': line.strip()[:300]}
        if route:
            item['route'] = route
        if symbol:
            item['symbols'] = [symbol]
        if aliases:
            item['aliases'] = list(dict.fromkeys(a for a in aliases if a and a.lower() != name.lower()))
        found.append(item)

    def line_at(index):
        start = text.rfind('\n', 0, index) + 1
        end = text.find('\n', index)
        return text[start:end if end >= 0 else len(text)]

    suffix = PurePosixPath(path).suffix
    route = _file_route(path)
    if route is not None:
        stem = PurePosixPath(path).parent.name if PurePosixPath(path).stem in ('page', '+page', 'index') else PurePosixPath(path).stem
        add(_words(stem.strip('[]')) if route != '/' else 'Home', text.split('\n', 1)[0], route=route)
    if suffix in {'.js', '.jsx', '.mjs', '.ts', '.tsx', '.vue', '.svelte', '.html'}:
        for match in RENDER_FUNCTION.finditer(text):
            verb, rest = (match.group(1), match.group(2)) if match.group(1) else (match.group(3), match.group(4))
            if verb == 'show' and not SCREEN_SUFFIX.search(rest):
                continue
            add(_words(rest), line_at(match.start()), symbol=verb + rest)
        for match in COMPONENT.finditer(text):
            add(_words(match.group(1)) or _words(match.group(2)), line_at(match.start()),
                symbol=match.group(1) + match.group(2))
        for regex in (ROUTER_ENTRY, JSX_ROUTE):
            for match in regex.finditer(text):
                add(_words(re.sub(r'(Page|Screen|View)$', '', match.group(2))) or _words(match.group(2)),
                    line_at(match.start()), route=match.group(1), symbol=match.group(2))
        labels = {}
        for match in VIEW_LABEL.finditer(text):
            label = ' '.join(re.sub(r'<[^>]*>', ' ', match.group(2)).split())[:60]
            if label:
                labels.setdefault(match.group(1).lower(), label)
        for match in VIEW_SECTION.finditer(text):
            key = match.group(1)
            label = labels.get(key.lower())
            add(label or _words(key), line_at(match.start()), route='#' + key, symbol='view-' + key,
                aliases=[_words(key)] if label else ())
    if suffix == '.py':
        for match in PY_ROUTE.finditer(text):
            body = re.split(r'\n(?=@|def |async def |class )', text[match.end():match.end() + 2000], 1)[0]
            route, function = match.group(1), match.group(2)
            if route.startswith(('/api', '/static')) or not PY_HTML.search(body):
                continue
            add(_words(function.removeprefix('page_').removeprefix('view_')), line_at(match.start()),
                route=route, symbol=function)
    return found


def detect(folder, paths=None) -> list[dict]:
    folder = Path(folder)
    candidates = []
    for path in (paths if paths is not None else source_files(folder)):
        if PurePosixPath(path).suffix not in SOURCE_SUFFIXES or any(p in SKIP for p in PurePosixPath(path).parts):
            continue
        candidates.extend(detect_text(path, _read(folder, path)))
    return candidates


def _same(screen, candidate) -> bool:
    if set(screen.get('symbols', [])) & set(candidate.get('symbols', [])):
        return True
    if candidate.get('route') and candidate['route'] in (screen.get('route'), screen.get('preview')):
        return True
    return (_norm(screen['name']) == _norm(candidate['name'])
            and bool(set(screen.get('files', [])) & set(candidate.get('files', []))))


def generate(folder, existing=None) -> tuple[dict, int]:
    """The map with newly detected screens added. Screens already listed are never changed."""
    screens = [dict(s) for s in (existing or {}).get('screens', [])]
    added = 0
    for candidate in detect(folder):
        if any(_same(s, candidate) for s in screens):
            match = next(s for s in screens if _same(s, candidate))
            if match.get('generated'):   # still a draft: complete it with what else was found
                for field in ('files', 'symbols', 'aliases'):
                    values = list(dict.fromkeys(match.get(field, []) + candidate.get(field, [])))
                    if values:
                        match[field] = values
                if candidate.get('route') and not match.get('route'):
                    match['route'] = candidate['route']
            continue
        if len(screens) >= MAX_SCREENS:
            break
        entry = {k: v for k, v in candidate.items() if k != 'line'}
        entry.setdefault('aliases', [])
        entry['generated'] = True
        screens.append(entry)
        added += 1
    return {'version': 1, 'screens': screens}, added


def write(folder, data) -> Path:
    path = path_of(folder)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return path


def _terms(screen):
    terms = [screen['name'], *screen.get('aliases', [])]
    terms += [s for s in screen.get('symbols', []) if len(s) >= 5]
    for route in (screen.get('route'), screen.get('preview')):
        if route and len(route) >= 3 and route not in ('/', '#'):
            terms.append(route)
    return [t for t in dict.fromkeys(terms) if len(t.strip()) >= 3]


def _hit(term: str, text: str) -> bool:
    needle = _norm(term).strip()
    if not needle:
        return False
    if needle[0] in '/#' or '_' in term or re.search(r'[a-z][A-Z]', term):
        return needle in text   # routes and function names: literally
    # Words: whole words, but the last word may continue (Dutch compounds: instellingenpagina).
    pattern = r'(?<![0-9a-zà-ÿ])' + re.escape(needle) + (r'' if len(needle) >= 5 else r'(?![0-9a-zà-ÿ])')
    return re.search(pattern, text) is not None


def match(data, task: str, limit=4) -> list[dict]:
    if not data or not task:
        return []
    text = _norm(task)
    scored = []
    for screen in data.get('screens', []):
        hits = [t for t in _terms(screen) if _hit(t, text)]
        if hits:
            scored.append((max(len(h) for h in hits), screen))
    scored.sort(key=lambda item: -item[0])
    return [screen for _, screen in scored[:limit]]


def describe(screen) -> str:
    parts = []
    if screen.get('route'):
        parts.append('route ' + screen['route'])
    if screen.get('aliases'):
        parts.append('also called ' + ', '.join(screen['aliases'][:5]))
    head = screen['name'] + (f" ({'; '.join(parts)})" if parts else '')
    details = []
    for field, label in (('files', 'files'), ('symbols', 'functions'), ('api', 'API'), ('tests', 'tests')):
        if screen.get(field):
            details.append(label + ' ' + ', '.join(screen[field][:8]))
    return head + (': ' + '; '.join(details) if details else '')


def context(data, task: str, limit=1800) -> str:
    """Prompt text for a task, or '' when the project has no map."""
    if not data:
        return ''
    upkeep = (f'If you add, rename or remove a screen, update {FILE} in the same change (name, aliases in the '
              'words the user uses, route, files, functions, tests).')
    found = match(data, task)
    if found:
        lines = [f'SCREENS NAMED IN THIS TASK (from {FILE}; orientation only, check the code itself):']
        lines += ['- ' + describe(s) for s in found]
        if len(found) > 1:
            lines.append('Several screens match; work on the one the task means and leave the others alone.')
        lines.append('Start in these files instead of searching the whole repository. ' + upkeep)
    elif SCREEN_WORDS.search(task or '') and data.get('screens'):
        names = [s['name'] + (f" ({s['route']})" if s.get('route') else '')
                 + (f" – {s['files'][0]}" if s.get('files') else '') for s in data['screens']]
        lines = [f'SCREENS OF THIS PROJECT (from {FILE}; the task names none of them directly, pick the one it '
                 'means): ' + '; '.join(names), upkeep]
    else:
        lines = [upkeep]
    text = '\n'.join(lines)
    return '\n' + (text if len(text) <= limit else text[:limit].rsplit(';', 1)[0] + ' …')


def uncovered(folder, data, added: dict) -> list[dict]:
    """Screens this change adds (in its added lines) that the map does not list."""
    if not data:
        return []
    result = []
    for path, lines in (added or {}).items():
        if path == FILE:
            continue
        stripped = {line.strip()[:300] for line in lines}
        for candidate in detect(folder, [path]):
            if candidate['line'] in stripped and not any(_same(s, candidate) for s in data.get('screens', [])):
                result.append(candidate)
    return result


def for_files(data, paths) -> list[dict]:
    """Screens whose files include one of these paths (most specific first)."""
    if not data:
        return []
    paths = set(paths or ())
    hits = [s for s in data.get('screens', []) if paths & set(s.get('files', []))]
    return sorted(hits, key=lambda s: len(s.get('files', [])))


def refresh(folder, data) -> tuple[dict | None, list[dict]]:
    """The map with file paths that no longer exist looked up again, and what was stale.

    A screen's file may be renamed or moved between planning and review. A missing path is searched
    again by file name: exactly one file with that name means it moved (the map is used with the new
    path), otherwise the path is dropped. Returns (map to use, [{'screen', 'path', 'now'}]); the file
    on disk is not changed, the agent is asked to update it in its change.
    """
    if not data:
        return data, []
    folder = Path(folder)
    stale, by_name, screens = [], None, []
    for screen in data.get('screens', []):
        fixed = dict(screen)
        for field in ('files', 'tests'):
            kept = []
            for path in screen.get(field, []):
                if (folder / path).exists():
                    kept.append(path)
                    continue
                if by_name is None:
                    by_name = {}
                    for found in tracked_like(folder):
                        by_name.setdefault(PurePosixPath(found).name, []).append(found)
                moved = by_name.get(PurePosixPath(path).name, [])
                now = moved[0] if len(moved) == 1 else None
                stale.append({'screen': screen['name'], 'path': path, 'now': now})
                if now and now not in kept:
                    kept.append(now)
            if field in screen:
                fixed[field] = kept
        screens.append(fixed)
    return {**data, 'screens': screens}, stale


def tracked_like(folder) -> list[str]:
    """Every file below the project (skipping dependency and build folders), as posix paths."""
    folder = Path(folder)
    found = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith('.')]
        for name in files:
            found.append((Path(root) / name).relative_to(folder).as_posix())
            if len(found) >= MAX_FILES * 5:
                return found
    return found


def stale_note(stale) -> str:
    if not stale:
        return ''
    parts = [f"{s['path']} ({s['screen']})" + (f" is now {s['now']}" if s['now'] else ' no longer exists')
             for s in stale[:8]]
    return (f'STALE PATHS in {FILE}: ' + '; '.join(parts) + f'. Do not rely on those entries; find the current '
            f'files in the code and correct {FILE} in this change.')
