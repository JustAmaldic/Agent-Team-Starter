#!/usr/bin/env python3
"""ATS entry point, called from an application repository with .ats clone."""
import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import tempfile
import socket
import fnmatch
import hashlib
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import ats_intelligence as intelligence
from ats_modules import checkpoints
from ats_modules import dag as dag_planning

from ats_contract import accepted_event, valid_phase

ATS = Path(__file__).resolve().parent


def app_directory():
    """The application ATS works on.

    ATS_APP lets one central ATS installation (for example the ATS app's host)
    work on any project; without it the classic `.ats` clone layout applies.
    """
    configured = os.environ.get('ATS_APP', '').strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return ATS.parent if ATS.name == '.ats' else Path.cwd()


APP = app_directory()

SUPPORTED_AGENTS = ('codex', 'claude', 'gemini', 'opencode', 'antigravity')
DEFAULT_AGENTS = ('codex', 'claude')


def agents_config_path():
    return APP / '.ats-agents.json'


def enabled_agents(include_blocked=False):
    """Return selectable agents; keep configured roster intact across temporary quota limits."""
    path = agents_config_path()
    if not path.exists():
        configured = list(DEFAULT_AGENTS)
    else:
        try:
            config = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f'Invalid {path}: {exc}') from exc
        configured = config.get('enabled') if isinstance(config, dict) else None
        if not isinstance(configured, list) or not configured or any(
            not isinstance(a, str) or a not in SUPPORTED_AGENTS for a in configured
        ) or len(configured) != len(set(configured)):
            raise RuntimeError(f'{path}: enabled must be a non-empty list of unique supported agents')
    if include_blocked:
        return configured
    blocked = {name.strip().lower() for name in os.environ.get('ATS_BLOCKED_AGENTS', '').split(',')
               if name.strip()}
    return [name for name in configured if name not in blocked]


def configure_agents(enable=(), disable=()):
    selected = enabled_agents(include_blocked=True)
    for name in (*enable, *disable):
        if name not in SUPPORTED_AGENTS:
            raise RuntimeError(f'Unknown agent: {name}')
    for name in enable:
        if name not in selected:
            selected.append(name)
    selected = [name for name in selected if name not in disable]
    if not selected:
        raise RuntimeError('At least one agent must remain enabled')
    path = agents_config_path()
    path.write_text(json.dumps({'enabled': selected}, indent=2) + '\n', encoding='utf-8')
    return selected


def alternate_agent(agent, unavailable=()):
    return next((name for name in enabled_agents() if name != agent and name not in unavailable), None)
def state_directory():
    """Keep project-specific agent output outside the Git repository."""
    root = Path(os.environ.get('ATS_STATE_DIR', str(Path.home() / '.ats-state'))).expanduser()
    project_id = hashlib.sha256(str(APP.resolve()).encode('utf-8')).hexdigest()[:20]
    return root / project_id / 'data'


def private_data_repo():
    """Opt into the user's separate private data checkout when present."""
    value = os.environ.get('ATS_DATA_REPO')
    folder = Path(value).expanduser().resolve() if value else Path.home() / '.ats-data'
    if folder.exists():
        if not (folder / '.git').exists():
            raise RuntimeError('ATS private data folder is not a Git clone: ' + str(folder))
        return folder
    if value:
        raise RuntimeError('ATS_DATA_REPO does not exist: ' + str(folder))
    return None


PRIVATE_DATA_REPO = private_data_repo()
DATA = PRIVATE_DATA_REPO / 'data' if PRIVATE_DATA_REPO else state_directory()


GITHUB_REMOTE_URL = re.compile(
    r'^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)'
    r'([A-Za-z0-9-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?$', re.IGNORECASE)
GITHUB_OWNER_REPO = re.compile(r'^([A-Za-z0-9-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?$')


def github_repo(value, allow_bare=False):
    """Return 'owner/repo' for a GitHub remote URL (or a bare owner/repo), else None."""
    text = (value or '').strip()
    match = GITHUB_REMOTE_URL.match(text) or (allow_bare and GITHUB_OWNER_REPO.match(text))
    return match.group(1) if match else None


def data_remote_repo(root):
    """owner/repo of the private data repository.

    ATS_DATA_REMOTE (owner/repo or GitHub URL) pins the expected repository.
    Without it, the data clone's own origin is used, but never the ATS source
    repository itself. Privacy is verified separately via GitHub.
    """
    remote = execute(['git', 'remote', 'get-url', 'origin'], root).stdout.strip()
    repo = github_repo(remote)
    if not repo:
        raise RuntimeError('Refusing to send private events to an unexpected remote '
                           '(not a GitHub repository): ' + remote)
    configured = os.environ.get('ATS_DATA_REMOTE', '').strip()
    if configured:
        expected = github_repo(configured, allow_bare=True)
        if not expected:
            raise RuntimeError('ATS_DATA_REMOTE must be owner/repo or a GitHub URL: ' + configured)
        if repo.lower() != expected.lower():
            raise RuntimeError('Refusing to send private events to an unexpected remote: ' +
                               remote + ' (ATS_DATA_REMOTE is ' + expected + ')')
        return repo
    source = execute(['git', 'remote', 'get-url', 'origin'], ATS, check=False)
    source_repo = github_repo(source.stdout) if source.returncode == 0 else None
    if source_repo and source_repo.lower() == repo.lower():
        raise RuntimeError('Refusing to send private events to an unexpected remote: the data '
                           'clone points at the ATS source repository ' + remote)
    return repo


def validate_private_remote(root):
    repo = data_remote_repo(root)
    gh = shutil.which('gh')
    if not gh:
        raise RuntimeError('GitHub CLI (gh) is required to verify the ATS data repository is private.')
    visibility = execute([gh, 'repo', 'view', repo,
                          '--json', 'visibility', '--jq', '.visibility'], root).stdout.strip()
    if visibility.upper() != 'PRIVATE':
        raise RuntimeError(repo + ' is not verified PRIVATE; refusing synchronization.')
    return gh


DATA_LOCK_WAIT = 180          # seconds a task waits for another task's ATS-data sync
DATA_LOCK_STALE = 15 * 60     # an ATS lock (or git index.lock) this old belongs to a process that died


class data_repo_lock:
    """One ATS process at a time in the shared ATS-data checkout.

    Parallel tasks (the ATS app runs up to max_parallel) all write events and then sync the same
    ~/.ats-data; without this they collide on git's index.lock ("Another git process seems to be
    running"). A git index.lock left by a crashed git is removed only when it is old and we hold
    the ATS lock, so no ATS git can be using it.
    """

    def __init__(self, root):
        git_dir = Path(root) / '.git'
        usable = git_dir.is_dir()   # a .git file (worktree) or no clone: nothing shared to protect
        self.path = git_dir / 'ats-sync.lock' if usable else None
        self.index_lock = git_dir / 'index.lock'

    def __enter__(self):
        if self.path is None:
            return self
        deadline = time.time() + DATA_LOCK_WAIT
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                break
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > DATA_LOCK_STALE:
                        self.path.unlink(missing_ok=True)
                        continue
                except OSError:
                    continue
                if time.time() > deadline:
                    raise RuntimeError('ATS-data is busy with another task (lock ' + str(self.path) + ').')
                time.sleep(1)
        try:
            if time.time() - self.index_lock.stat().st_mtime > DATA_LOCK_STALE:
                self.index_lock.unlink()
                print('ATS: removed a stale git index.lock in ATS-data.', file=sys.stderr)
        except OSError:
            pass
        return self

    def __exit__(self, *exc):
        if self.path is not None:
            self.path.unlink(missing_ok=True)
        return False


def sync_private_data():
    """Publish private events in a PR; keep main clean and require human merge."""
    if PRIVATE_DATA_REPO is None:
        raise RuntimeError('Clone your private ATS data repository into ~/.ats-data or set ATS_DATA_REPO.')
    with data_repo_lock(PRIVATE_DATA_REPO):
        return _sync_private_data()


DATA_FOLDERS = ('data', 'knowledge')


def data_sync_branch():
    """One rolling sync branch (and PR) per computer, so syncs never pile up as conflicting PRs."""
    host = re.sub(r'[^a-z0-9-]', '-', socket.gethostname().lower()).strip('-')[:40] or 'host'
    return 'ats-data/sync-' + host


def pending_data(root):
    """{path: bytes or None (deleted)} for local data that is not on main yet."""
    status = execute(['git', 'status', '--porcelain', '--untracked-files=all', '-z'], root).stdout
    pending, entries = {}, status.split('\0')
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in 'RC':   # rename/copy: the original path follows
            index += 1
        if not path.replace('\\', '/').startswith(tuple(f + '/' for f in DATA_FOLDERS)):
            raise RuntimeError('Unexpected changes in ATS-data outside data/ and knowledge/: ' + path)
        file = Path(root) / path
        pending[path] = file.read_bytes() if file.is_file() else None
    return pending


def _write_pending(root, pending):
    for path, content in pending.items():
        file = Path(root) / path
        if content is None:
            file.unlink(missing_ok=True)
        else:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(content)


def _refresh_private_knowledge():
    """Fast-forward the local main to origin/main while keeping every local, unpublished change.

    Local data that is identical to origin/main (because its PR was merged) simply becomes clean.
    Local versions of files that also changed upstream are kept (they are newer, local decisions).
    """
    root = PRIVATE_DATA_REPO
    validate_private_remote(root)
    branch = execute(['git', 'branch', '--show-current'], root).stdout.strip()
    if branch != 'main':
        raise RuntimeError(f'ATS-data is on {branch!r}, not main. Switch it back with: '
                           f'git -C "{root}" switch main (data on that branch is in its pull request).')
    execute(['git', 'fetch', '--quiet', 'origin', 'main'], root, timeout=120)
    pending = pending_data(root)
    tracked = [p for p in pending if execute(['git', 'cat-file', '-e', f'HEAD:{p}'], root, check=False).returncode == 0]
    if tracked:
        execute(['git', 'checkout', 'HEAD', '--', *tracked], root)
    for path in pending:
        if path not in tracked:
            (Path(root) / path).unlink(missing_ok=True)
    try:
        merged = execute(['git', 'merge', '--ff-only', '--quiet', 'origin/main'], root, check=False)
    finally:
        _write_pending(root, pending)   # never lose local data, whatever happened above
    if merged.returncode:
        raise RuntimeError('ATS-data main cannot fast-forward to origin/main (local commits on main?): '
                           + (merged.stderr or merged.stdout)[-300:])
    return pending_data(root)


DATA_PUSH_ATTEMPTS = 3


def _publish_commit(root, pending, ref):
    """A commit of origin/main plus the local data, made in a temporary worktree (local main untouched)."""
    publish = Path(tempfile.mkdtemp(prefix='ats-data-publish-'))
    execute(['git', 'worktree', 'add', '--force', '--detach', str(publish), ref], root)
    _write_pending(publish, pending)
    execute(['git', 'add', '-A', '--', *DATA_FOLDERS], publish)
    execute(['git', *git_identity(publish), 'commit', '--quiet', '-m',
             f'ATS: private events and learned knowledge ({len(pending)} files)'], publish)
    return publish


def _drop_worktree(root, publish):
    execute(['git', 'worktree', 'remove', '--force', str(publish)], root, check=False)
    shutil.rmtree(publish, ignore_errors=True)
    execute(['git', 'worktree', 'prune'], root, check=False)


def _sync_private_data():
    """Push private data directly to main, never create a data PR.

    Concurrent pushes are retried against the newest main. Failure retains
    pending local files and leaves the application task outcome unchanged.
    """
    if PRIVATE_DATA_REPO is None:
        raise RuntimeError('Clone your private ATS data repository into ~/.ats-data or set ATS_DATA_REPO.')
    root = PRIVATE_DATA_REPO
    gh = validate_private_remote(root)
    repo = data_remote_repo(root)
    pending = _refresh_private_knowledge()
    if not pending:
        print('ATS: private knowledge up to date.', flush=True)
        return
    last_problem = ''
    for _ in range(DATA_PUSH_ATTEMPTS):
        publish = _publish_commit(root, pending, 'origin/main')
        try:
            pushed = execute(['git', 'push', '--quiet', 'origin', 'HEAD:main'],
                             publish, check=False, timeout=120)
        finally:
            _drop_worktree(root, publish)
        if pushed.returncode == 0:
            pending = _refresh_private_knowledge()
            print('ATS: private knowledge pushed directly to ' + repo + '/main'
                  + (' (' + str(len(pending)) + ' newer local files pending)'
                     if pending else '') + '.', flush=True)
            _close_old_sync_pr(gh, repo, data_sync_branch(), root)
            return
        last_problem = (pushed.stderr or pushed.stdout or 'unknown git push error').strip()
        lowered = last_problem.lower()
        if any(term in lowered for term in ('non-fast-forward', 'fetch first', 'stale info',
                                            'failed to push some refs')):
            pending = _refresh_private_knowledge()
            if not pending:
                return
            continue
        break
    raise RuntimeError(
        'ATS-data direct push to ' + repo + '/main failed. No PR was created; '
        'local events and knowledge are retained for retry with .ats data-sync. '
        'Check write permissions and main branch protection. Git: ' + last_problem[-400:]
    )


def _close_old_sync_pr(gh, repo, branch, root):
    """After a direct push, a sync PR left over from the PR mode is no longer needed."""
    number = execute([gh, 'pr', 'list', '--repo', repo, '--head', branch, '--state', 'open',
                      '--json', 'number', '--jq', '.[0].number'], root, check=False).stdout.strip()
    if number:
        execute([gh, 'pr', 'close', number, '--repo', repo, '--delete-branch', '--comment',
                 'ATS now pushes ATS-data straight to main; this data is already there.'], root, check=False)


def refresh_private_knowledge():
    """Bring in merged knowledge before a task, keeping local unpublished data."""
    if PRIVATE_DATA_REPO is None:
        return
    with data_repo_lock(PRIVATE_DATA_REPO):
        _refresh_private_knowledge()


def auto_sync_private_data():
    """Best-effort direct data push after tasks; never hide the application's result."""
    if PRIVATE_DATA_REPO is None:
        print('ATS: no private ATS-data clone configured; task history stays local.',
              file=sys.stderr)
        return
    try:
        sync_private_data()
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
        print('ATS: automatic data sync failed. Local events are retained in ' +
              str(PRIVATE_DATA_REPO) + '. Run .ats data-sync after resolving: ' +
              str(exc), file=sys.stderr)


def execute(argv, cwd=None, check=True, capture=True, timeout=None):
    p = subprocess.run(argv, cwd=cwd or APP, text=True, capture_output=capture, check=False, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError(f"{argv[0]} exited {p.returncode}: {(p.stderr or p.stdout or '')[-1200:]}")
    return p

def git(*args, check=True):
    return execute(['git', *args], ATS, check=check)

def cleanname(s):
    return re.sub(r'[^a-zA-Z0-9_-]', '-', s)[:70]

def project():
    p = APP / '.ats-project.json'
    try:
        return json.loads(p.read_text()) if p.exists() else {'project': APP.name, 'test_commands': [], 'max_revisions': 2}
    except json.JSONDecodeError as e:
        raise RuntimeError(f'Invalid {p}: {e}')

def sync_in():
    """Read-only update of ATS source code; never send local project data."""
    if not (ATS / '.git').exists():
        raise RuntimeError('ATS must be a separately cloned Git repository.')
    dirty = git('status', '--porcelain', '--untracked-files=all').stdout.strip()
    if dirty:
        raise RuntimeError(
            'ATS checkout has uncommitted changes. Resolve them before updating; '
            'ATS will never auto-commit or upload local project history: ' + dirty[:250]
        )
    branch = git('branch', '--show-current').stdout.strip()
    if not branch:
        raise RuntimeError('ATS must be on a named branch to synchronize.')
    git('fetch', 'origin', branch)
    git('merge', '--ff-only', f'origin/{branch}')


def sync_out():
    """Project history remains on the local machine, never pushed to GitHub."""
    return


def read_json(path):
    """Read ATS JSON written as UTF-8; tolerate legacy Windows cp1252 event files."""
    raw = Path(path).read_bytes()
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = raw.decode('cp1252', errors='replace')
    return json.loads(text)


EMIT_LOCK = threading.Lock()


def emit(item):
    """Append one event as a JSON line to ATS_EVENTS_FILE for a live consumer.

    Best effort: a missing variable or an unwritable file never affects the task.
    """
    path = os.environ.get('ATS_EVENTS_FILE')
    if not path:
        return
    try:
        line = json.dumps(item, ensure_ascii=False, default=str) + '\n'
        with EMIT_LOCK, open(path, 'a', encoding='utf-8') as handle:   # parallel parts emit from two threads
            handle.write(line)
            handle.flush()
    except OSError:
        pass


def phase(stage, task_id, **fields):
    """Live-only stage marker (not stored as an event file)."""
    if not valid_phase(stage):
        raise ValueError('Unknown ATS event phase: ' + str(stage))
    emit(dict(timestamp=dt.datetime.now(dt.timezone.utc).isoformat(), task_id=task_id,
              event_type='phase', stage=stage, **fields))


def record(kind, summary, task=None, **fields):
    conf = project()
    name = cleanname(conf.get('project', APP.name))
    target = DATA / name / 'events'
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    uid = str(uuid.uuid4())
    item = dict(timestamp=now, project=name, task_id=task or uid, event_type=kind,
                summary=summary, event_id=uid, **fields)
    if not accepted_event(item, persisted=True):
        raise ValueError('Invalid ATS persistent event envelope')
    (target / f'{now.replace(":", "-")}-{uid}.json').write_text(
        json.dumps(item, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    emit(item)
    return item

KNOWLEDGE_KINDS = {'finding', 'feedback', 'lesson', 'rule', 'decision', 'pattern'}
CONFIDENCE_LEVELS = ('low', 'medium', 'high')
KNOWLEDGE_PRIORITY = {'rule': 0, 'decision': 1, 'lesson': 2, 'pattern': 3, 'feedback': 4, 'finding': 5}


# A learning that would make agents do something drastic always needs a human. Everything else that
# the review process produces is approved automatically (standing decision of the owner, POLICY.md).
RISK_PATTERNS = (
    (r'rm\s+-rf|rmtree|reset\s+--hard|git\s+clean|filter-branch|rebase\s+-i', 'destructive git/file command'),
    (r'force[- ]?push|push\s+(?:-f\b|--force)|--force-with-lease', 'force push'),
    (r'\b(?:delete|remove|wipe|purge|drop|truncate|verwijder|wis)\b[^.\n]{0,40}'
     r'\b(?:branch|worktree|repo|repository|database|table|data|files?|folders?|map|bestanden|history|geschiedenis)\b',
     'deletes data'),
    (r'\b(?:skip|disable|bypass|ignore|overslaan|uitzetten|negeer)\b[^.\n]{0,30}'
     r'\b(?:review|tests?|checks?|security|auth|sandbox|approval|goedkeuring|beveiliging)\b',
     'weakens review, tests or security'),
    (r'\b(?:without|zonder)\s+(?:review|tests?|checks?|approval|goedkeuring)', 'weakens review, tests or security'),
    (r'(?:push|merge|commit)\w*\s+(?:\w+\s+){0,2}(?:to|into|naar|op)\s+main\b|auto-?merge', 'touches main directly'),
    (r'--dangerously|approve-for-me\s+--yolo|--yolo', 'unsafe agent flags'),
    (r'\b(?:credentials?|secrets?|passwords?|wachtwoord|api[- ]?keys?|private\s+key)\b', 'credentials'),
    (r'\b(?:real money|echt geld|live (?:account|trading)|place (?:an )?orders?|deploy(?:ment)? to production)\b',
     'money or production'),
)
LEARNING_SCOPES = ('project', 'shared')


def learning_risk(*texts):
    """Why a learning needs a human, or None when it is low risk."""
    text = ' '.join(str(t) for t in texts)
    for pattern, reason in RISK_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return reason
    return None


def knowledge_base():
    return PRIVATE_DATA_REPO / 'knowledge' if PRIVATE_DATA_REPO else state_directory().parent / 'knowledge'


def known_titles():
    """Titles of the project's and shared knowledge, to avoid storing the same learning twice."""
    name = cleanname(project().get('project', APP.name))
    titles = set()
    for folder in (knowledge_base() / 'projects' / name, knowledge_base() / 'shared'):
        for path in folder.rglob('*.json') if folder.exists() else ():
            try:
                item = read_json(path)
            except (ValueError, OSError):
                continue
            if item.get('status') != 'rejected' and item.get('title'):
                titles.add(' '.join(str(item['title']).lower().split()))
    return titles


def learn(kind, title, observation, evidence, task_id=None, confidence='medium', created_by='human',
          scope='project', auto=False, risk=None):
    """Store an evidence-linked learning.

    auto=True (learnings from the review process): approved right away unless it is risky, in which
    case it stays 'proposed' with the reason in needs_human. Otherwise it is a proposal.
    """
    if kind not in KNOWLEDGE_KINDS:
        raise RuntimeError('Invalid learning kind: ' + kind)
    if not evidence.strip():
        raise RuntimeError('Learning requires a supporting evidence reference.')
    if confidence not in CONFIDENCE_LEVELS:
        raise RuntimeError('Invalid confidence: ' + str(confidence))
    if scope not in LEARNING_SCOPES:
        raise RuntimeError('Invalid scope: ' + str(scope))
    conf = project()
    name = 'shared' if scope == 'shared' else cleanname(conf.get('project', APP.name))
    if not name:
        raise RuntimeError('Project name is required.')
    uid = str(uuid.uuid4())
    now = dt.datetime.now(dt.timezone.utc)
    filename = now.strftime('%Y%m%dT%H%M%SZ') + '-' + uid + '.json'
    sections = {'finding': 'findings', 'feedback': 'feedback', 'lesson': 'lessons',
                'rule': 'rules', 'decision': 'decisions', 'pattern': 'patterns'}
    target = (knowledge_base() / 'shared' if scope == 'shared' else knowledge_base() / 'projects' / name) / sections[kind]
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    item = {'id': uid, 'project': name, 'kind': kind,
            'title': title, 'observation': observation, 'evidence': [evidence],
            'status': 'proposed', 'confidence': confidence,
            'created_at': now.isoformat(), 'created_by': created_by, 'task_id': task_id}
    if auto:
        reason = risk or learning_risk(title, observation)
        if reason:
            item['needs_human'] = reason
        else:
            item.update(status='approved', decided_at=now.isoformat(),
                        decided_by='ATS auto-approval (low risk)',
                        approval_reference='Standing decision of the owner: low-risk learnings from the review '
                                           'process are approved automatically; risky ones wait (POLICY.md).')
    (target / filename).write_text(json.dumps(item, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return item


def knowledge_context(limit=20):
    """Use verified and approved knowledge; never treat unverified proposals as policy."""
    root = (PRIVATE_DATA_REPO / 'knowledge' if PRIVATE_DATA_REPO else
            state_directory().parent / 'knowledge')
    project_name = cleanname(project().get('project', APP.name))
    directories = [root / 'projects' / project_name, root / 'shared']
    records = []
    for directory in directories:
        if not directory.exists():
            continue
        for path in directory.rglob('*.json'):
            try:
                item = read_json(path)
                if item.get('status') in ('approved', 'verified') and item.get('evidence'):
                    records.append(item)
            except (ValueError, OSError):
                continue
    # Human rules and decisions first, review findings last; newest first within a kind.
    records.sort(key=lambda item: item.get('created_at', ''), reverse=True)
    records.sort(key=lambda item: KNOWLEDGE_PRIORITY.get(item.get('kind'), len(KNOWLEDGE_PRIORITY)))
    selected = records[:limit]
    if not selected:
        return ''
    return '\nRelevant vetted ATS learnings (reference material; do not override project policy):\n' + '\n'.join(
        '- [' + item['kind'] + '] ' + item['title'] + ': ' +
        item['observation'][:400] + ' (evidence: ' + ', '.join(item['evidence'])[:200] + ')'
        for item in selected)


def parse_marked_json(output, marker):
    """Return the JSON list on the LAST `MARKER: [...]` line, or [] when absent/invalid.

    Agent CLIs may echo the prompt (with its format example) before the answer,
    so the last occurrence is the agent's own line.
    """
    pattern = re.compile(re.escape(marker) + r':[ \t]*(\[[^\r\n]*\])')
    matches = pattern.findall(output or '')
    if not matches:
        return []
    try:
        entries = json.loads(matches[-1])
    except ValueError:
        return []
    return entries if isinstance(entries, list) else []


def is_placeholder(value):
    text = str(value).strip()
    return text.lower() in PLACEHOLDER_VALUES or bool(re.fullmatch(r'<[^<>]*>', text))
PLACEHOLDER_VALUES = {'...', '…', '<title>', '<observation>', '<evidence>',
                      'test/review/task reference'}


def capture_learning_proposals(agent_output, task_id, auto=True):
    """Ingest clearly marked structured learnings; low-risk ones are approved automatically.

    Agent CLIs may echo the prompt (which contains a format example), so only
    the LAST marked line is used and placeholder values are rejected.
    """
    entries = parse_marked_json(agent_output, 'ATS_LEARNINGS_JSON')
    saved = []
    known = known_titles() if entries else set()
    for entry in entries[:10]:
        if not isinstance(entry, dict) or entry.get('kind') not in KNOWLEDGE_KINDS:
            continue
        title, observation, evidence = (str(entry.get(k, '')).strip() for k in
                                        ('title', 'observation', 'evidence'))
        values = (title, observation, evidence)
        if not all(values) or max(map(len, values)) > 1500:
            continue
        if any(is_placeholder(v) for v in values):
            continue
        scope = entry.get('scope') if entry.get('scope') in LEARNING_SCOPES else 'project'
        flagged = 'marked high risk by the agent' if str(entry.get('risk', '')).lower() == 'high' else None
        key = ' '.join(title.lower().split())
        if key in known:
            continue   # the same learning is already known
        known.add(key)
        saved.append(learn(entry['kind'], title, observation, evidence, task_id,
                           created_by='agent', scope=scope, auto=auto, risk=flagged))
    return saved


from ats_modules.agent_limits import (
    AgentUsageExhausted, BothAgentsExhausted, AgentSandboxBlocked,
    USAGE_PATTERNS, TRANSIENT_PATTERNS, RESET_PATTERN, RATE_LIMIT_BACKOFF,
    SANDBOX_PATTERNS, is_hard_usage_limit, is_transient_rate_limit,
    is_usage_exhausted, usage_reset_hint, sandbox_blocked,
)


def codex_exec_command():
    """Use Codex's approval-aware workspace sandbox, including on Windows.

    The prompt goes through stdin ('-'), never as an argument: on Windows codex/claude are .cmd
    shims run by cmd.exe, which caps the command line at 8191 characters (a review prompt with a
    diff is far longer: "The command line is too long") and mangles newlines and quotes.
    """
    return agent_command('codex', 'exec', '--approve-for-me', '-')


def claude_print_command():
    """Claude Code in print mode; without a prompt argument it reads the prompt from stdin."""
    return agent_command('claude', '-p', '--permission-mode', 'acceptEdits')


def codex_preflight(cwd, quiet=False):
    """Ask Codex itself to read policy and write a disposable file."""
    marker = 'ATS_PREFLIGHT_OK'
    probe = Path(cwd) / '.ats-codex-preflight'
    probe.unlink(missing_ok=True)
    prompt = (f'Read {ATS / "POLICY.md"} and {ATS / "AGENTS.md"}. '
              f'Create the file {probe} with exactly {marker} and no other text. '
              'Do not modify any other files. Reply briefly when finished.')
    try:
        result = stream_command(
            codex_exec_command(),
            cwd, timeout=120, quiet=quiet, stdin_text=prompt)
        output = (result.stdout or '') + '\n' + (result.stderr or '')
        if result.returncode or not probe.exists() or probe.read_text(encoding='utf-8').strip() != marker:
            if is_usage_exhausted(output):
                raise AgentUsageExhausted('codex', usage_reset_hint(output))
            raise AgentSandboxBlocked(
                'Codex preflight failed: cannot confirm policy read and workspace write. '
                + output[-700:])
    finally:
        probe.unlink(missing_ok=True)


def invoke_with_fallback(agent, prompt, cwd, unavailable, task_id, purpose, quiet=False,
                         fallback=True):
    selected = enabled_agents()
    if agent not in selected:
        raise RuntimeError(f'Agent {agent} is disabled. Configure it with .ats agents --enable {agent}')
    candidates = [agent] + ([name for name in selected if name != agent] if fallback else [])
    reasons, resets = {}, []
    for candidate in candidates:
        if candidate in unavailable:
            reasons[candidate] = unavailable_reason(unavailable, candidate)
            continue
        if not executable(candidate):
            mark_unavailable(unavailable, candidate, 'missing CLI')
            reasons[candidate] = 'missing CLI'
            continue
        try:
            if not quiet:
                print(f'ATS: {candidate} starting {purpose}...', flush=True)
            if candidate == 'codex' and purpose == 'implementation':
                if not quiet:
                    print('ATS: checking Codex policy access and sandbox write...', flush=True)
                codex_preflight(cwd, quiet=quiet)
            result = invoke(candidate, prompt, cwd, quiet=quiet, **({'purpose': purpose} if candidate == 'antigravity' else {}))
            if candidate == 'codex' and sandbox_blocked(result):
                raise AgentSandboxBlocked('Codex reported a sandbox or policy block.')
            if not quiet:
                print(f'ATS: {candidate} finished {purpose}.', flush=True)
            return candidate, result
        except AgentSandboxBlocked as exc:
            mark_unavailable(unavailable, candidate, 'sandbox')
            reasons[candidate] = 'sandbox'
            print(f'ATS: {candidate} blocked: {exc}; trying alternate agent.', file=sys.stderr)
            record('sandbox_blocked', str(exc)[:1200], task_id, agent=candidate, phase=purpose)
        except AgentUsageExhausted as exc:
            mark_unavailable(unavailable, candidate, 'usage')
            reasons[candidate] = 'usage'
            if exc.resets:
                resets.append(f'{candidate}: {exc.resets}')
            print(str(exc), file=sys.stderr)
            record('usage_exhausted', str(exc), task_id, agent=candidate, phase=purpose,
                   resets=exc.resets)
    if reasons and all(reason == 'usage' for reason in reasons.values()):
        if not fallback:
            raise AgentUsageExhausted(agent, resets[0].split(': ', 1)[-1] if resets else None)
        raise BothAgentsExhausted('; '.join(resets) or None)
    raise RuntimeError('No usable enabled agent available: ' +
                       ', '.join(f'{a} {r}' for a, r in sorted(reasons.items())))


class UnavailableAgents(set):
    """Set of agents that dropped out of a task, remembering why."""
    def __init__(self, *args):
        super().__init__(*args)
        self.reasons = {}


def mark_unavailable(unavailable, agent, reason):
    unavailable.add(agent)
    if isinstance(unavailable, UnavailableAgents):
        unavailable.reasons[agent] = reason


def unavailable_reason(unavailable, agent):
    return getattr(unavailable, 'reasons', {}).get(agent, 'usage')


def claude_authenticated(output):
    try:
        status = json.loads(output)
        return bool(status.get('loggedIn') or status.get('authenticated'))
    except (ValueError, TypeError):
        return 'logged in' in output.lower() and 'not logged in' not in output.lower()


def executable(name):
    if name == 'antigravity':
        name = 'agy'
    if os.name == 'nt' and name in ('npm', 'codex', 'claude', 'gemini', 'opencode', 'agy'):
        return shutil.which(name + '.cmd') or shutil.which(name + '.exe') or shutil.which(name)
    return shutil.which(name)


def agent_command(agent, *args):
    return [executable(agent) or agent, *args]


def run_check(command, cwd, timeout=900):
    """Run a project test command.

    Strings are split shell-style on macOS/Linux (no shell, so a config string
    such as "python3 -m pytest -q" works) and run through the shell on Windows.
    Lists are always executed directly. Timeouts and missing executables become
    a failed check instead of aborting the task.
    """
    use_shell = os.name == 'nt' and isinstance(command, str)
    if isinstance(command, str):
        argv = command if use_shell else shlex.split(command)
    else:
        argv = [str(part) for part in command]
    try:
        return subprocess.run(argv, cwd=cwd, text=True, encoding='utf-8', errors='replace',
                              capture_output=True, check=False, timeout=timeout,
                              shell=use_shell)
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout if isinstance(exc.stdout, str) else ''
        return subprocess.CompletedProcess(argv, 124, output or '',
                                           f'ATS: test command timed out after {timeout}s')
    except (FileNotFoundError, PermissionError) as exc:
        return subprocess.CompletedProcess(argv, 127, '', f'ATS: cannot run test command: {exc}')


def install_requirements():
    """Install Python dependencies first, then check system prerequisites."""
    if sys.version_info < (3, 10):
        raise RuntimeError('ATS requires Python 3.10 or newer')
    requirements = ATS / 'requirements.txt'
    if requirements.exists():
        packages = [line.strip() for line in requirements.read_text(encoding='utf-8').splitlines()
                    if line.strip() and not line.lstrip().startswith('#')]
        if packages:
            print('ATS: installing Python requirements...', flush=True)
            execute([sys.executable, '-m', 'pip', 'install', '-r', str(requirements)],
                    cwd=ATS, capture=False)
        else:
            print('ATS: no third-party Python packages required.', flush=True)

    if not shutil.which('git'):
        raise RuntimeError('Git is required for ATS. Install Git and retry.')

    if not executable('npm'):
        if os.name == 'nt':
            manager = shutil.which('winget')
            if not manager:
                raise RuntimeError('Node.js/npm missing. Install Node.js LTS (winget unavailable), reopen PowerShell and retry .ats install.')
            print('ATS: Node.js/npm missing; installing Node.js LTS with winget...', flush=True)
            execute([manager, 'install', '--id', 'OpenJS.NodeJS.LTS', '--exact',
                     '--source', 'winget', '--accept-package-agreements',
                     '--accept-source-agreements'], capture=False)
            if not executable('npm'):
                raise RuntimeError('Node.js installation completed but npm is not on PATH. Open a new terminal and rerun .ats install.')
        elif shutil.which('brew'):
            print('ATS: Node.js/npm missing; installing Node.js with Homebrew...', flush=True)
            execute(['brew', 'install', 'node'], capture=False)
            if not executable('npm'):
                raise RuntimeError('Node.js installed but npm is not on PATH. Open a new terminal and rerun .ats install.')
        else:
            raise RuntimeError('Node.js/npm missing. Install Node.js LTS and rerun .ats install.')



def install_agents():
    selected = enabled_agents()
    print('ATS: configuring enabled agents: ' + ', '.join(selected), flush=True)
    if 'codex' in selected and not executable('codex'):
        if executable('npm'):
            execute([executable('npm'), 'install', '-g', '@openai/codex'], capture=False)
        elif shutil.which('brew'):
            execute(['brew', 'install', '--cask', 'codex'], capture=False)
        else:
            raise RuntimeError('Install Node.js/npm or Homebrew before installing Codex')
    if 'codex' in selected:
        if not executable('codex'):
            raise RuntimeError('Codex installed but not on PATH; reopen terminal and retry')
        if execute(agent_command('codex', 'login', 'status'), check=False).returncode:
            execute(agent_command('codex', 'login'), capture=False)
        if execute(agent_command('codex', 'login', 'status'), check=False).returncode:
            raise RuntimeError('Codex login not completed')
        print('Codex ready.', flush=True)
    if 'claude' in selected and not executable('claude'):
        if shutil.which('brew'):
            execute(['brew', 'install', '--cask', 'claude-code'], capture=False)
        elif executable('npm'):
            execute([executable('npm'), 'install', '-g', '@anthropic-ai/claude-code'], capture=False)
        else:
            raise RuntimeError('Install Homebrew or Node.js/npm before installing Claude Code')
    if 'claude' in selected:
        if not executable('claude'):
            raise RuntimeError('Claude Code installed but not on PATH; reopen terminal and retry')
        result = execute(agent_command('claude', 'auth', 'status'), check=False)
        if result.returncode or not claude_authenticated(result.stdout):
            execute(agent_command('claude', 'auth', 'login'), capture=False)
        result = execute(agent_command('claude', 'auth', 'status'), check=False)
        if result.returncode or not claude_authenticated(result.stdout):
            raise RuntimeError('Claude Code login not completed')
    for name in selected:
        if name in ('gemini', 'opencode', 'antigravity') and not executable(name):
            raise RuntimeError(f'{name} CLI missing: install it and authenticate before using ATS (Antigravity uses the agy command)')
    print('Enabled agents ready.', flush=True)


def stream_command(command, cwd, timeout=1800, quiet=False, stdin_text=None):
    """Stream both process pipes while retaining full output for ATS checks.

    `stdin_text` (the agent prompt) is written to the process's stdin and then closed.
    """
    process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True,
                               stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                               encoding='utf-8', errors='replace', bufsize=1)
    chunks = {'stdout': [], 'stderr': []}
    def reader(pipe, key, target):
        try:
            for line in iter(pipe.readline, ''):
                chunks[key].append(line)
                if not quiet:
                    print(line, end='', file=target, flush=True)
        finally:
            pipe.close()

    threads = [
        threading.Thread(target=reader, args=(process.stdout, 'stdout', sys.stdout), daemon=True),
        threading.Thread(target=reader, args=(process.stderr, 'stderr', sys.stderr), daemon=True),
    ]
    if stdin_text is not None:
        def feed():
            try:
                process.stdin.write(stdin_text)
            except (BrokenPipeError, OSError, ValueError):
                pass   # the process ended early; its exit code tells what happened
            finally:
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass
        threads.append(threading.Thread(target=feed, daemon=True))
    for thread in threads:
        thread.start()
    try:
        process.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        process.kill()
        process.wait()
        raise
    finally:
        for thread in threads:
            thread.join()
    return subprocess.CompletedProcess(command, process.returncode,
                                       ''.join(chunks['stdout']), ''.join(chunks['stderr']))


def parse_antigravity_result(stdout, stderr='', returncode=0):
    """The agy JSON status is authoritative even on exit code zero."""
    try:
        payload = json.loads(stdout.strip())
    except (ValueError, TypeError) as exc:
        raise RuntimeError('Antigravity did not return valid headless JSON: ' + str(exc)) from exc
    if not isinstance(payload, dict):
        raise RuntimeError('Antigravity returned an unexpected JSON payload.')
    status = str(payload.get('status', '')).upper()
    response = payload.get('response')
    if status != 'SUCCESS' or returncode:
        message = str(payload.get('error') or stderr or response or 'unknown error')[-1400:]
        if is_usage_exhausted(message):
            raise AgentUsageExhausted('antigravity', usage_reset_hint(message))
        if sandbox_blocked(message) or 'permission' in message.lower() or 'soft-denied' in message.lower():
            raise AgentSandboxBlocked('Antigravity blocked a tool or permission: ' + message)
        raise RuntimeError(f'Antigravity headless status {status or "unknown"}: ' + message)
    if not isinstance(response, str) or not response.strip():
        raise RuntimeError('Antigravity finished without an actionable text response.')
    if 'soft-denied' in stderr.lower() or 'permission denied' in stderr.lower() or 'requires approval' in stderr.lower():
        raise AgentSandboxBlocked('Antigravity tool permissions require explicit configuration: ' + stderr[-800:])
    return response


def invoke_antigravity(prompt, cwd, quiet=False, backoff=None, purpose='implementation'):
    """Call Google's headless agy CLI with explicit safety modes and structured result checks."""
    binary = executable('antigravity')
    if not binary:
        raise RuntimeError('Antigravity CLI (agy) missing; install and authenticate it first.')
    if purpose in ('review', 'planning'):
        mode = 'plan'
    else:
        mode = 'accept-edits'
    command = [binary, '-p', prompt, '--output-format', 'json',
               '--mode=' + mode, '--print-timeout', '25m']
    model = selected_agent_model('antigravity') or os.environ.get('ATS_ANTIGRAVITY_MODEL', '').strip()
    if model:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', model):
            raise RuntimeError('Invalid Antigravity model slug')
        command.extend(['--model', model])
    delays = RATE_LIMIT_BACKOFF if backoff is None else backoff
    attempt = 0
    while True:
        result = stream_command(command, cwd, timeout=1800, quiet=True)
        combined = (result.stdout or '') + '\n' + (result.stderr or '')
        if result.returncode and is_transient_rate_limit(combined) and attempt < len(delays):
            time.sleep(delays[attempt])
            attempt += 1
            continue
        text = parse_antigravity_result(result.stdout, result.stderr, result.returncode)
        if not quiet:
            print(text, flush=True)
        return text


def selected_agent_model(agent):
    value = os.environ.get('ATS_MODEL_' + agent.upper(), '').strip()
    if not value:
        return None
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/:-]{0,127}', value):
        raise RuntimeError(f'Invalid configured model ID for {agent}')
    return value


def invoke(agent, prompt, cwd, quiet=False, backoff=None, purpose='implementation'):
    if agent == 'antigravity':
        return invoke_antigravity(prompt, cwd, quiet=quiet, backoff=backoff, purpose=purpose)
    commands = {
        'codex': codex_exec_command,
        'claude': claude_print_command,
        'gemini': lambda: agent_command('gemini', '-p', ''),
        'opencode': lambda: agent_command('opencode', 'run'),
    }
    if agent not in commands:
        raise RuntimeError(f'Unsupported agent: {agent}')
    command = commands[agent]()
    model = selected_agent_model(agent)
    if model:
        if agent == 'codex':
            command[-1:-1] = ['--model', model]
        else:
            command.extend(['--model', model])
    if agent == 'opencode':
        command.append(prompt)
    if not executable(agent):
        raise RuntimeError(f'{agent} executable missing; run .ats install')
    delays = RATE_LIMIT_BACKOFF if backoff is None else backoff
    attempt = 0
    while True:
        p = stream_command(command, cwd, timeout=1800, quiet=quiet,
                           stdin_text=None if agent == 'opencode' else prompt)
        out = (p.stdout or '')+'\n'+(p.stderr or '')
        if p.returncode and is_transient_rate_limit(out) and attempt < len(delays):
            print(f'ATS: {agent} is rate limited; retrying in {delays[attempt]}s '
                  f'({attempt + 1}/{len(delays)}).', file=sys.stderr, flush=True)
            time.sleep(delays[attempt])
            attempt += 1
            continue
        break
    if p.returncode:
        if sandbox_blocked(out):
            raise AgentSandboxBlocked(f'{agent} sandbox blocked: {out[-1200:]}')
        if is_usage_exhausted(out):
            raise AgentUsageExhausted(agent, usage_reset_hint(out))
        raise RuntimeError(f'{agent} error: {out[-1800:]}')
    if sandbox_blocked(out):
        raise AgentSandboxBlocked(f'{agent} sandbox blocked: {out[-1200:]}')
    return out


def select(task):
    conf=project()
    scores={name: 0 for name in enabled_agents()}
    if not scores:
        raise RuntimeError('No available agents: check the host weekly usage limits.')
    keywords={'codex':['test','bug','python','fix'], 'claude':['architecture','design','refactor']}
    keywords.update(conf.get('routing',{}))
    for agent, words in keywords.items():
        if agent in scores: scores[agent] += sum(2 for x in words if x.lower() in task.lower())
    events=sorted((DATA / cleanname(conf.get('project', APP.name)) / 'events').glob('*.json'))[-100:]
    for file in events:
        try:
            e=read_json(file); agent=e.get('implementer')
            if agent in scores and e.get('event_type')=='review_result':
                scores[agent] += 1 if e.get('approved') else -1
        except (ValueError,OSError): pass
    metrics = intelligence.performance(intelligence.read_events(DATA / cleanname(conf.get('project', APP.name)) / 'events'))
    relevant = set(intelligence.categories(task))
    for row in metrics:
        if row['reliable'] and row['agent'] in scores and row['category'] in relevant:
            scores[row['agent']] += round(4 * row['approval_rate'] - min(3, row['revisions'] / row['samples']))
    return max(scores,key=lambda n:(scores[n],n==conf.get('default_agent','codex')))

NON_GIT_EXCLUDED_NAMES = {
    '.ats', '.git', '.venv', 'venv', 'node_modules', '__pycache__',
    '.pytest_cache', '.mypy_cache', '.tox', '.next', 'dist', 'build',
    '.DS_Store', '.idea', '.vscode', '.ssh', '.aws', '.config',
}
NON_GIT_SECRET_PATTERNS = (
    '.env', '.env.*', '*.pem', '*.key', '*.p12', '*.pfx',
    '*credentials*.json', '*token*.json', 'id_rsa*', 'id_ed25519*',
)


def non_git_copy_ignore(directory, names):
    """Exclude ATS, generated folders and likely credentials from local snapshots."""
    excluded = set()
    for name in names:
        if name in NON_GIT_EXCLUDED_NAMES:
            excluded.add(name)
        elif name != '.env.example' and any(
            fnmatch.fnmatch(name.lower(), pattern) for pattern in NON_GIT_SECRET_PATTERNS
        ):
            excluded.add(name)
    return excluded


def make_local_workspace(folder, branch):
    """Create a disposable Git history outside the user's non-Git project."""
    shutil.copytree(APP, folder, ignore=non_git_copy_ignore, symlinks=False)
    execute(['git', 'init', '-b', 'main'], folder)
    execute(['git', 'add', '-A'], folder)
    execute(['git', '-c', 'user.name=ATS', '-c', 'user.email=ats@localhost',
             'commit', '--allow-empty', '-m', 'ATS: local workspace snapshot'], folder)
    execute(['git', 'switch', '-c', branch], folder)


def export_local_patch(folder):
    """Create a reviewable patch; never modify the original project automatically."""
    patch = APP.parent / (folder.name + '.patch')
    diff = execute(['git', 'diff', '--binary', 'main', 'HEAD'], folder).stdout
    if diff:
        patch.write_text(diff, encoding='utf-8')
    return patch if diff else None


# ---------------------------------------------------------------- review loop
SEVERITIES = ('BLOCKER', 'MAJOR', 'MINOR', 'SUGGESTION')
BLOCKING_SEVERITIES = ('BLOCKER', 'MAJOR')
SEVERITY_ALIASES = {'CRITICAL': 'BLOCKER', 'HIGH': 'MAJOR', 'MEDIUM': 'MINOR', 'LOW': 'MINOR',
                    'NIT': 'SUGGESTION', 'INFO': 'SUGGESTION'}
RESPONSE_STATUSES = ('fixed', 'not_applicable', 'deferred')
VERDICT_LINE = re.compile(r'^[\W_]*VERDICT:\s*(APPROVE|CHANGES_REQUESTED)[\W_]*$',
                          re.IGNORECASE | re.MULTILINE)
GENERATED_PATH_PATTERNS = ('__pycache__/', '.pyc', '.pytest_cache/', '.mypy_cache/',
                           '.ruff_cache/', 'node_modules/', '.coverage', 'coverage.xml',
                           '.ats-codex-preflight')


def parse_verdict(review):
    """The verdict is the LAST line that consists only of VERDICT: APPROVE|CHANGES_REQUESTED."""
    matches = VERDICT_LINE.findall(review or '')
    return matches[-1].upper() if matches else None


def normalize_findings(entries):
    findings = []
    for number, entry in enumerate(entries[:50], 1):
        if not isinstance(entry, dict):
            continue
        summary = str(entry.get('summary') or entry.get('issue') or '').strip()
        if not summary or any(is_placeholder(v) for v in entry.values() if isinstance(v, str) and v):
            continue
        raw = str(entry.get('severity', '')).strip().upper()
        severity = raw if raw in SEVERITIES else SEVERITY_ALIASES.get(raw, 'MAJOR')
        findings.append({'id': str(entry.get('id') or f'F{number}').strip()[:20],
                         'severity': severity,
                         'file': str(entry.get('file', '')).strip()[:200],
                         'line': str(entry.get('line', '')).strip()[:40],
                         'summary': summary[:500]})
    return findings


def normalize_responses(entries):
    responses = []
    for entry in entries[:50]:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get('status', '')).strip().lower().replace(' ', '_').replace('-', '_')
        finding_id = str(entry.get('id', '')).strip()
        if status not in RESPONSE_STATUSES or not finding_id or is_placeholder(finding_id):
            continue
        responses.append({'id': finding_id[:20], 'status': status,
                          'note': str(entry.get('note', '')).strip()[:500],
                          'reference': str(entry.get('reference', '')).strip()[:200]})
    return responses


def evaluate_approval(verdict, checks, findings, history, reviewer, implementer, conf,
                      tampered=False):
    """Every reason in the returned list blocks approval (POLICY.md feedback loop)."""
    reasons = []
    if tampered:
        reasons.append('reviewer modified the workspace (changes reverted)')
    if not reviewer or reviewer == implementer:
        reasons.append('no independent reviewer')
    if verdict != 'APPROVE':
        reasons.append('verdict: ' + (verdict or 'missing VERDICT line'))
    failed = [c['command'] for c in checks if not c['passed']]
    if failed:
        reasons.append('failing checks: ' + ', '.join(map(str, failed)))
    if not checks and not conf.get('allow_approval_without_tests', False):
        reasons.append('no test_commands configured (set allow_approval_without_tests to override)')
    open_blocking = [f['id'] for f in findings if f['severity'] in BLOCKING_SEVERITIES]
    if open_blocking:
        reasons.append('open BLOCKER/MAJOR findings: ' + ', '.join(open_blocking))
    for past in history:
        answered = {r['id']: r['status'] for r in past.get('responses', [])}
        for finding in past['findings']:
            if finding['severity'] not in BLOCKING_SEVERITIES:
                continue
            status = answered.get(finding['id'])
            if status is None:
                reasons.append(f"unanswered {finding['severity']} {finding['id']} "
                               f"(cycle {past['cycle']})")
            elif status == 'deferred' and finding['severity'] == 'BLOCKER':
                reasons.append(f"deferred BLOCKER {finding['id']} (cycle {past['cycle']})")
    return reasons


def index_tree(folder):
    execute(['git', 'add', '-A'], folder)
    return execute(['git', 'write-tree'], folder).stdout.strip()


def restore_if_changed(folder, tree):
    """Revert anything the reviewer changed. Returns the changed non-generated paths."""
    after = index_tree(folder)
    if after == tree:
        return []
    changed = execute(['git', 'diff', '--name-only', tree, after], folder).stdout.split()
    execute(['git', 'read-tree', '--reset', '-u', tree], folder)
    return [path for path in changed
            if not any(marker in path for marker in GENERATED_PATH_PATTERNS)]


def run_checks(conf, folder):
    checks = []
    for cmd in conf.get('test_commands', []):
        outcome = run_check(cmd, folder, timeout=int(conf.get('test_timeout', 900)))
        checks.append({'command': cmd, 'passed': outcome.returncode == 0,
                       'output': ((outcome.stdout or '') + (outcome.stderr or ''))[-1000:]})
    return checks


STYLE_FILES = ('AGENTS.md', 'CLAUDE.md', 'CONTRIBUTING.md', 'STYLEGUIDE.md', 'STYLE.md', 'DESIGN.md',
               'docs/style.md', 'docs/STYLE.md', 'docs/design.md', '.editorconfig', 'pyproject.toml', 'setup.cfg',
               'ruff.toml', '.ruff.toml', '.flake8', '.prettierrc', '.prettierrc.json', '.prettierrc.js',
               'prettier.config.js', '.eslintrc', '.eslintrc.json', '.eslintrc.js', 'eslint.config.js',
               'eslint.config.mjs', 'biome.json', '.stylelintrc', '.stylelintrc.json', 'tailwind.config.js',
               'tailwind.config.ts', 'tsconfig.json')
STYLE_SKIP = {'node_modules', '.git', 'dist', 'build', '.venv', 'venv', '.venv-bouw', 'vendor', '__pycache__',
              '.next', 'coverage', 'site-packages'}
STYLE_LIMIT = 2500
_style_cache = {}


def _style_sheets(folder, limit=300):
    found = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in STYLE_SKIP and not d.startswith('.')]
        for name in files:
            if name.endswith(('.css', '.scss', '.less')) and not name.endswith('.min.css'):
                found.append(Path(root) / name)
                if len(found) >= limit:
                    return found
    return found


def style_context(folder, conf=None):
    """A short brief so agents keep the project's existing code and visual style.

    Lists the style sources the project has (guides, formatter/linter configs) for the agent to read,
    the design tokens (CSS custom properties) and fonts the UI already uses, and the project's own
    style_guide notes from .ats-project.json. Cached per worktree.
    """
    folder = Path(folder)
    key = str(folder.resolve())
    if key in _style_cache:
        return _style_cache[key]
    sources = [name for name in STYLE_FILES if (folder / name).is_file()]
    tokens, fonts, token_file = [], {}, None
    for sheet in _style_sheets(folder):
        try:
            text = sheet.read_text(encoding='utf-8', errors='replace')[:200000]
        except OSError:
            continue
        found = re.findall(r'(--[\w-]+)\s*:\s*([^;{}\n]{1,60});', ' '.join(re.findall(r':root[^{]*\{([^}]*)\}', text)))
        if len(found) > len(tokens):
            tokens, token_file = found, sheet.relative_to(folder).as_posix()
        for family in re.findall(r'font-family\s*:\s*([^;{}\n]{1,80})', text):
            fonts[family.strip()] = fonts.get(family.strip(), 0) + 1
    lines = ['MATCH THE EXISTING STYLE. Before writing, read two or three existing files like the ones you '
             'change and follow their conventions: naming, structure, formatting, comments, error handling, '
             'test style and the language and tone of user-facing text. Reuse existing components, helpers, '
             'CSS classes and design tokens. Do not introduce new colours, fonts, spacing or radius values, UI '
             'libraries, frameworks or code styles unless the task asks for it; extend what is there instead.']
    if sources:
        lines.append('Style sources in this project (read the relevant ones): ' + ', '.join(sources) + '.')
    if tokens:
        lines.append(f'Design tokens in {token_file} (use these instead of literal values): '
                     + '; '.join(f'{n}: {v.strip()}' for n, v in tokens[:40]) + '.')
    if fonts:
        lines.append('Fonts in use: ' + ' | '.join(f for f, _ in sorted(fonts.items(), key=lambda x: -x[1])[:3]) + '.')
    notes = (conf or {}).get('style_guide')
    if isinstance(notes, list):
        notes = '\n'.join(str(n) for n in notes)
    if isinstance(notes, str) and notes.strip():
        lines.append('Project style notes: ' + notes.strip()[:1500])
    text = '\n' + '\n'.join(lines)
    if len(text) > STYLE_LIMIT:
        text = text[:STYLE_LIMIT].rsplit(';', 1)[0] + ' …'
    _style_cache[key] = text
    return text


def review_prompt(task, checks, diff, history, style=''):
    previous = ''
    if history:
        last = history[-1]
        answered = {r['id']: r for r in last.get('responses', [])}
        items = [dict(f, response=answered.get(f['id'])) for f in last['findings']]
        previous = (f"This is re-review cycle {len(history)}. Previous open findings with the "
                    "author's responses (null = no answer):\n" + json.dumps(items, ensure_ascii=False) +
                    "\nVerify every one against the diff. Keep a finding open (same id) unless it is "
                    "really resolved; an unanswered BLOCKER/MAJOR or a deferred BLOCKER stays open.\n")
    return (f"Review ONLY these application changes for task {task!r} using {ATS/'POLICY.md'}.\n"
            "REVIEW-ONLY MODE: do not create, edit or delete any file. ATS checks the workspace "
            "afterwards; any change is reverted and the review is rejected.\n"
            "Inspect the diff and test results below yourself; do not rely on the implementer's summary.\n"
            "Also check consistency with the existing project: code that departs from its conventions, or UI "
            "that introduces new colours, fonts, spacing, components or libraries instead of the existing "
            "ones, is a MINOR finding; visibly inconsistent UI or a new framework/dependency without need is MAJOR."
            + style + "\n"
            + previous +
            "Write, in this order:\n"
            "1. Findings in prose: severity (BLOCKER, MAJOR, MINOR, SUGGESTION), file:line, issue, why it matters, concrete fix.\n"
            "2. ONE LINE listing only findings that are still open, or [] if none, in this shape: "
            'ATS_FINDINGS_JSON: [{"id":"<id>","severity":"<severity>","file":"<path>","line":"<line>","summary":"<summary>"}]\n'
            "3. Optionally ONE LINE ATS_LEARNINGS_JSON with evidence-backed reusable lessons (no secrets).\n"
            "4. As the very last line, exactly one verdict line (APPROVE only when no BLOCKER/MAJOR is open), "
            "either VERDICT: APPROVE or VERDICT: CHANGES_REQUESTED.\n"
            'Checks: ' + json.dumps(checks, ensure_ascii=False) + '\nDiff:\n' + diff)


def revision_prompt(task, review, findings, style=''):
    return (f"Revise your implementation for task {task!r}. Never commit, push, merge, deploy or modify .ats.\n"
            "Address the review below. Answer EVERY finding with status fixed (with test/file "
            "evidence), not_applicable (with evidence why) or deferred (with an issue/reference). "
            "Deferred BLOCKERs and unanswered BLOCKER/MAJOR findings prevent approval.\n"
            "End with ONE LINE in this shape: "
            'ATS_RESPONSES_JSON: [{"id":"<id>","status":"<status>","note":"<evidence>","reference":"<reference>"}]\n'
            'Keep matching the existing style of the project.' + style + '\n'
            'Open findings: ' + json.dumps(findings, ensure_ascii=False) +
            '\nReview:\n' + review[-12000:])


def review_loop(task, task_id, folder, branch, implementer, unavailable, conf, quiet=False,
                base='HEAD', subtask=None):
    """Independent review with enforced read-only reviewer and POLICY feedback loop.

    Returns (approved, reviewer, implementer, history).
    """
    context = {'subtask': subtask} if subtask is not None else {}
    max_revisions = int(conf.get('max_revisions', 2))
    max_chars = int(conf.get('max_review_diff_chars', 150000))
    history = []
    reviewer = None
    reasons = ['not reviewed']
    for cycle in range(max_revisions + 1):
        phase('checks', task_id, **context, cycle=cycle)
        checks = run_checks(conf, folder)
        # Repair a narrowly failing test before spending tokens on a full diff review.
        # A successful repair still goes through the independent review below.
        failed = [c for c in checks if not c['passed']]
        if failed and cycle < max_revisions and conf.get('targeted_test_fix', True):
            phase('revision', task_id, **context, cycle=cycle, implementer=implementer)
            problem = json.dumps([{'command': c['command'], 'output': c['output'][-1200:]}
                                  for c in failed], ensure_ascii=False)
            fix_prompt = (f'Read {ATS/"POLICY.md"} and {ATS/"AGENTS.md"}. '
                          f'Task: {task}\\nOnly repair these failing checks in the EXISTING worktree: '
                          + problem + '\\nKeep already working functionality. Do not restart the task, '
                          'rewrite unrelated files, commit, push or modify .ats. '
                          'After editing, state what you changed.')
            try:
                implementer, response = invoke_with_fallback(
                    implementer, fix_prompt, folder, unavailable, task_id, 'revision',
                    quiet=quiet, fallback=False)
                record('targeted_test_fix', response[-1200:], task_id, **context,
                       implementer=implementer, failed_commands=[c['command'] for c in failed])
                checks = run_checks(conf, folder)
            except (AgentUsageExhausted, RuntimeError) as exc:
                record('targeted_test_fix_skipped', str(exc)[:500], task_id, **context)
                # Keep existing behavior: the full independent review still decides.
        execute(['git', 'add', '-A'], folder)
        # Against the commit (or, for a subtask, the tree) the work started from, not HEAD: an agent
        # that runs `git commit` itself would otherwise make its own work invisible.
        diff = execute(['git', 'diff', '--cached', base], folder).stdout
        if not diff.strip():
            record('blocked', 'Agent made no changes; worktree preserved for diagnosis', task_id, **context,
                   implementer=implementer, branch=branch)
            raise RuntimeError('Agent made no changes; worktree preserved for diagnosis: ' + str(folder))
        preferred = alternate_agent(implementer, unavailable)
        if preferred is None:
            message = 'Independent review unavailable: no other enabled agent is available'
            print(message, file=sys.stderr)
            record('review_skipped', message, task_id, **context, implementer=implementer, reviewer=preferred)
            reasons = ['no independent reviewer available']
            break
        if len(diff) > max_chars:
            message = (f'Diff is {len(diff)} characters, above max_review_diff_chars={max_chars}; '
                       'refusing to review a truncated diff. Split the task or raise the limit.')
            print('ATS: ' + message, file=sys.stderr)
            record('review_skipped', message, task_id, **context, implementer=implementer, reviewer=preferred)
            reasons = ['diff too large to review completely']
            break
        tree = index_tree(folder)
        phase('review', task_id, **context, cycle=cycle, reviewer=preferred, implementer=implementer)
        try:
            reviewer, review = invoke_with_fallback(preferred, review_prompt(task, checks, diff, history, style_context(folder, conf)),
                                                    folder, unavailable, task_id, 'review',
                                                    quiet=quiet, fallback=False)
        except AgentUsageExhausted as exc:
            print(str(exc) + ': independent review skipped', file=sys.stderr)
            record('review_skipped', str(exc), task_id, **context, implementer=implementer,
                   reviewer=preferred, resets=exc.resets)
            reasons = ['reviewer usage exhausted']
            break
        except RuntimeError as exc:
            restore_if_changed(folder, tree)
            print(f'ATS: review by {preferred} failed: {exc}', file=sys.stderr)
            record('review_skipped', str(exc)[:1200], task_id, **context, implementer=implementer, reviewer=preferred)
            reasons = ['review failed']
            break
        tampered = restore_if_changed(folder, tree)
        if tampered:
            print(f'ATS: reviewer {reviewer} changed files in review-only mode; reverted: '
                  + ', '.join(tampered[:10]), file=sys.stderr)
        verdict = parse_verdict(review)
        findings = normalize_findings(parse_marked_json(review, 'ATS_FINDINGS_JSON'))
        reasons = evaluate_approval(verdict, checks, findings, history, reviewer, implementer,
                                    conf, tampered=bool(tampered))
        approved = not reasons
        capture_learning_proposals(review, task_id)
        record('review_result', review[-8000:], task_id, **context, implementer=implementer, reviewer=reviewer,
               approved=approved, verdict=verdict, findings=findings, blocking_reasons=reasons,
               tampered_paths=tampered, tests=checks, cycle=cycle, branch=branch)
        history.append({'cycle': cycle, 'reviewer': reviewer, 'findings': findings, 'responses': []})
        if approved:
            phase('approved', task_id, **context, cycle=cycle)
            return True, reviewer, implementer, history
        if cycle == max_revisions:
            break
        phase('revision', task_id, **context, cycle=cycle, implementer=implementer)
        try:
            implementer, response = invoke_with_fallback(implementer, revision_prompt(task, review, findings, style_context(folder, conf)),
                                                         folder, unavailable, task_id, 'revision', quiet=quiet)
        except AgentUsageExhausted as exc:
            print(str(exc), file=sys.stderr)
            record('blocked', str(exc), task_id, **context, phase='revision', resets=exc.resets)
            reasons = ['revision impossible: ' + str(exc)]
            break
        responses = normalize_responses(parse_marked_json(response, 'ATS_RESPONSES_JSON'))
        history[-1]['responses'] = responses
        answered = {r['id'] for r in responses}
        missing = [f['id'] for f in findings if f['id'] not in answered]
        record('feedback_response', response[-2000:], task_id, **context, implementer=implementer, cycle=cycle,
               responses=responses, unanswered=missing)
        capture_learning_proposals(response, task_id)
    summary = 'Not approved; needs a human decision: ' + '; '.join(reasons)
    print('ATS: ' + summary, file=sys.stderr)
    record('escalated', summary, task_id, **context, implementer=implementer, reviewer=reviewer,
           blocking_reasons=reasons, cycles=len(history), branch=branch)
    return False, reviewer, implementer, history


SUBTASK_EXAMPLE = ["first bounded implementation with tests", "next bounded implementation with tests"]


MAX_PARTS = 20
MAX_PARALLEL_PARTS = 2   # one part per agent


def plan_waves(plan):
    """Plan -> waves: lists of parts built at the same time. None when the plan is unusable.

    A string is a part on its own; a list of two strings is a pair the two agents build in
    parallel (parts that do not depend on each other). Waves run in order.
    """
    if isinstance(plan, dict) and plan.get('plan_version') == 2:
        try:
            return [[part['title'] for part in batch] for batch in dag_planning.execution_batches(plan)]
        except ValueError:
            return None
    if not isinstance(plan, list):
        return None
    waves = []
    for entry in plan:
        wave = [entry] if isinstance(entry, str) else entry
        if not isinstance(wave, list) or not 1 <= len(wave) <= MAX_PARALLEL_PARTS:
            return None
        if not all(isinstance(p, str) and p.strip() and not is_placeholder(p) and p not in SUBTASK_EXAMPLE
                   for p in wave):
            return None
        waves.append([p.strip() for p in wave])
    total = sum(len(w) for w in waves)
    return waves if 2 <= total <= MAX_PARTS else None


def valid_plan(plan):
    return plan_waves(plan) is not None


def subtask_plan(output):
    """The plan from an ATS_SUBTASKS_JSON line, or None when the agent implemented directly.

    A marker with an unusable plan is an error: the agent chose to split but gave no real parts.
    """
    dag_lines = re.findall(r'^\s*ATS_DAG_JSON:[^\r\n]*
    if dag_lines:
        try:
            return dag_planning.validate_dag(parse_marked_json(dag_lines[-1], 'ATS_DAG_JSON'))
        except ValueError as exc:
            raise RuntimeError('Invalid task DAG: ' + str(exc)) from exc
    lines = re.findall(r'^\s*ATS_SUBTASKS_JSON:[^\r\n]*$', output, re.MULTILINE)
    if not lines:
        return None
    plan = parse_marked_json(lines[-1], 'ATS_SUBTASKS_JSON')
    if not valid_plan(plan):
        raise RuntimeError('Invalid task plan: expected 2 to 20 concrete parts (strings, or pairs of strings)')
    return plan


def should_preplan(task, mode='auto'):
    """Decide before implementation whether to request a structured multi-agent plan."""
    if mode == 'never':
        return False
    if mode == 'always':
        return True
    text = task.strip()
    # Multi-feature requests and longer tasks deserve independent work packages.
    words = len(text.split())
    separate_goals = sum(text.lower().count(term) for term in (
        ' and ', ' also ', ' daarnaast ', ' bovendien ', ' weekly ',
        ' wekelijks ', ' en ', ';', '\n-', '\n1.', '\n2.'))
    return words >= 65 or (words >= 28 and separate_goals >= 2)


def preplan_task(task, task_id, folder, agent, unavailable, conf, quiet=False,
                 mode='auto'):
    """Plan before editing, validate, and make a safe execution decision.

    A complex task never silently falls through to an unbounded one-agent
    implementation if the planning response was missing or malformed.
    """
    if not should_preplan(task, mode):
        return None
    if len(enabled_agents()) < 2:
        raise RuntimeError('Automatic parallel planning requires two enabled agents.')
    print('ATS: planning bounded work before implementation...', flush=True)
    phase('planning', task_id, agent=agent)
    baseline = index_tree(folder)
    prompt = (
        f'Read {ATS/"POLICY.md"} and {ATS/"AGENTS.md"}. Plan ONLY, do not edit any files. '
        f'Project: {APP.name}. Task: {task}\n'
        'Inspect the code to divide this task into 2-20 concrete implementable parts '
        'with tests and independently reviewable outcomes. '
        'Return exactly ONE LINE starting ATS_DAG_JSON: followed by valid JSON. '
        'The JSON object has plan_version=2 and tasks (2-20 objects). Each task contains '
        'id, title, depends_on (list of prior task ids), and owned_paths (explicit source '
        'files or directories). Example: ATS_DAG_JSON: '
        '{"plan_version":2,"tasks":[{"id":"contract","title":"Define shared event contracts",'
        '"depends_on":[],"owned_paths":["contracts/"]},'
        '{"id":"scorer","title":"Implement sender safety scoring with regression tests",'
        '"depends_on":["contract"],"owned_paths":["phish.py","tests/test_phish.py"]},'
        '{"id":"audit","title":"Implement newsletter evidence inventory with tests",'
        '"depends_on":["contract"],"owned_paths":["newsletter_audit.py"]},'
        '{"id":"integration","title":"Integrate and test security and newsletter workflows",'
        '"depends_on":["scorer","audit"],"owned_paths":["mail_assist.py"]}]}. '
        'Declare ONLY true dependencies, not conceptual phases. '
        'Use separate owned_paths for independently implementable work. '
        'Optimize for the shortest safe critical path, not for the most sequential phases. '
        'Before choosing waves, identify each part\'s prerequisites, files it will own, '
        'and which parts can start immediately after a shared contract is fixed. '
        'Group independent parts into pairs EVEN WHEN they implement different layers '
        '(for example a data store and a standalone classifier), provided neither '
        'requires the other\'s unfinished interfaces and their write sets are disjoint. '
        'Do not make a part wait for an unrelated part merely because the latter belongs '
        'to an earlier conceptual phase. Put true prerequisites BEFORE dependent work. '
        'The execution engine has two agents and completes/reviews/merges an entire '
        'wave before starting the next, so minimize avoidable single-part waves. '
        'Prefer pairs over serial work when both tasks can run against existing '
        'interfaces or an agreed contract and independently pass tests. '
        'Never group parts that modify the same file, require each other\'s output, '
        'or depend on uncommitted code; if uncertain use consecutive strings. '
        'Plan one final integration and cross-feature test pass after component work. '
        'Never have two agents modify the same file concurrently. '
        'Every part needs a testable acceptance criterion; include file boundaries '
        'where they are known. Do not include credentials, raw email data, '
        'deployment, live trading, or deletion without explicit approval. '
        'Do not implement any changes. Do not add explanations outside the JSON line.'
    )
    last_problem = None
    for attempt in range(2):
        try:
            planner, output = invoke_with_fallback(
                agent, prompt + (f'\nYour prior plan was unusable: {last_problem}. '
                                 'Correct the JSON and return only the marker line.'
                                 if last_problem else ''),
                folder, unavailable, task_id, 'planning', quiet=quiet)
        finally:
            modified = restore_if_changed(folder, baseline)
            if modified:
                record('planning_warning',
                       'Planner modified files; reverted: ' + ', '.join(modified[:10]),
                       task_id, planner=agent)
        try:
            plan = subtask_plan(output)
        except RuntimeError as exc:
            last_problem = str(exc)
            continue
        if plan is not None:
            waves = plan_waves(plan)
            if mode == 'always' and not any(len(wave) == 2 for wave in waves):
                last_problem = 'No safe parallel pair was proposed'
                continue
            record('task_split', 'Validated preflight plan before implementation',
                   task_id, planner=planner, subtasks=plan,
                   parallel_pairs=sum(len(wave) == 2 for wave in waves))
            phase('planned', task_id, waves=len(waves),
                  parallel_pairs=sum(len(wave) == 2 for wave in waves))
            print(f'ATS: {sum(len(w) for w in waves)} parts in {len(waves)} waves; '
                  f'{sum(len(w) == 2 for w in waves)} parallel pairs.', flush=True)
            return plan
        last_problem = 'ATS_SUBTASKS_JSON marker missing'
    raise RuntimeError(
        'Preflight planner did not return a safe, valid task plan after two attempts: '
        + str(last_problem) + '. No implementation started.'
    )


def other_agent(agent):
    return alternate_agent(agent)


def git_identity(folder):
    """['-c', ...] for an ATS identity when git has none here (local snapshot projects), else []."""
    if execute(['git', 'config', 'user.email'], folder, check=False).stdout.strip():
        return []
    return ['-c', 'user.name=ATS', '-c', 'user.email=ats@localhost']


def commit_all(folder, message):
    """Commit everything in a worktree; returns False when there was nothing to commit."""
    execute(['git', 'add', '-A'], folder)
    if execute(['git', 'diff', '--cached', '--quiet'], folder, check=False).returncode == 0:
        return False
    execute(['git', *git_identity(folder), 'commit', '-m', message], folder)
    return True


def run_part(task, plan, number, total, part, task_id, folder, branch, implementer, unavailable, conf,
             base, quiet=False, alongside=None, resume_work=False):
    """Implement one part and put it through the review loop. Returns (approved, reviewer, author, cycles)."""
    description = f'{task}\nSubtask {number}/{total}: {part}'
    # The reviewer sees only this part's diff; with the plan it does not flag work that belongs
    # to another part as missing.
    review_task = (description + '\nFull plan (review ONLY this part; other parts are reviewed '
                   'separately): ' + json.dumps(plan, ensure_ascii=False))
    together = (f'Another agent builds part {alongside[0]} ({alongside[1]}) AT THE SAME TIME in a separate '
                'copy; do not implement it and avoid changing the same code. ' if alongside else '')
    phase('implementation', task_id, implementer=implementer, subtask=number)
    prompt = (f'Read {ATS/"POLICY.md"} and {ATS/"AGENTS.md"}. '
              f'Overall plan: {json.dumps(plan, ensure_ascii=False)}\n'
              f'Implement ONLY this part: {description}\n' + together +
              'Earlier parts have passed independent review. Preserve them. Add tests. '
              'Do not commit, push, merge, deploy, trade, expose secrets or modify .ats; '
              'ATS commits the work after the review.'
              + style_context(folder, conf)
              + knowledge_context()
              + '\nAt the end include ONE LINE ATS_LEARNINGS_JSON: [...] with evidence-backed '
              + 'items, or ATS_LEARNINGS_JSON: [] when there are none.')
    base_tree = execute(['git', 'rev-parse', str(base) + '^{tree}'], folder).stdout.strip()
    existing_changes = resume_work and index_tree(folder) != base_tree
    if existing_changes:
        author = implementer
        record('subtask_fix_only', 'Repairing unfinished changes in the resumed worktree',
               task_id, subtask=number, implementer=author)
    else:
        try:
            author, output = invoke_with_fallback(implementer, prompt, folder, unavailable,
                                                  task_id, 'implementation', quiet=quiet, fallback=False)
        except (AgentUsageExhausted, RuntimeError) as exc:
            record('escalated', str(exc), task_id, subtask=number, implementer=implementer, branch=branch,
                   blocking_reasons=[f'subtask {number}: {exc}'])
            return False, None, implementer, []
        record('implementation', output[-1600:], task_id, implementer=author, subtask=number)
        capture_learning_proposals(output, task_id)
    try:
        approved, reviewer, author, cycles = review_loop(
            review_task, task_id, folder, branch, author, unavailable, conf,
            quiet=quiet, base=base, subtask=number)
    except RuntimeError as exc:   # e.g. the part produced no changes
        record('escalated', str(exc), task_id, subtask=number, implementer=author, branch=branch,
               blocking_reasons=[f'subtask {number}: {exc}'])
        return False, None, author, []
    for cycle in cycles:
        cycle['subtask'] = number
        cycle['implementer'] = author
    if approved:
        record('subtask_completed', part, task_id, subtask=number, implementer=author,
               reviewer=reviewer, branch=branch)
    return approved, reviewer, author, cycles


def merge_parallel_part(task, plan, number, part, task_id, folder, branch, side_branch, author,
                        unavailable, conf, quiet=False):
    """Merge a part built in a side worktree into the task worktree.

    A clean merge with passing checks is done. Otherwise the agent that built the side part resolves
    the conflict (or fixes the failing checks) and the other agent reviews that resolution.
    Returns (approved, reviewer, cycles).
    """
    phase('merge', task_id, subtask=number)
    pre_merge = execute(['git', 'rev-parse', 'HEAD'], folder).stdout.strip()
    merged = execute(['git', *git_identity(folder), 'merge', '--no-ff', '--no-edit', '-m',
                      f'ATS: merge part {number}', side_branch], folder, check=False)
    if merged.returncode == 0:
        checks = run_checks(conf, folder)
        if all(c['passed'] for c in checks):
            record('subtask_merged', f'Part {number} merged cleanly', task_id, subtask=number, branch=branch)
            return True, None, []
        problem = ('The combined parts fail these checks; fix them without dropping either part:\n'
                   + json.dumps([{'command': c['command'], 'output': c['output'][-1500:]}
                                 for c in checks if not c['passed']], ensure_ascii=False))
    else:
        conflicts = execute(['git', 'diff', '--name-only', '--diff-filter=U'], folder, check=False).stdout.split()
        problem = ('Merging your part into the other part produced conflicts in: ' + ', '.join(conflicts[:30])
                   + '. Resolve every conflict marker so BOTH parts keep working.')
    record('merge_conflict', problem[:1200], task_id, subtask=number, implementer=author, branch=branch)
    prompt = (f'Read {ATS/"POLICY.md"} and {ATS/"AGENTS.md"}. Task: {task}\n'
              f'Overall plan: {json.dumps(plan, ensure_ascii=False)}\n'
              f'You built part {number} ({part}) in parallel with another part. {problem}\n'
              'Change only what the integration needs. Do not commit, push or modify .ats.'
              + style_context(folder, conf))
    try:
        fixer, output = invoke_with_fallback(author, prompt, folder, unavailable, task_id, 'implementation',
                                             quiet=quiet, fallback=False)
    except (AgentUsageExhausted, RuntimeError) as exc:
        record('escalated', str(exc), task_id, subtask=number, branch=branch,
               blocking_reasons=[f'merge of part {number}: {exc}'])
        return False, None, []
    record('implementation', output[-1600:], task_id, implementer=fixer, subtask=number)
    review_task = (f'{task}\nIntegration of part {number} ({part}) with the parts before it. The diff '
                   'is that part plus the merge resolution; check that both parts keep working.')
    try:
        approved, reviewer, fixer, cycles = review_loop(review_task, task_id, folder, branch, fixer, unavailable,
                                                        conf, quiet=quiet, base=pre_merge, subtask=number)
    except RuntimeError as exc:
        record('escalated', str(exc), task_id, subtask=number, branch=branch,
               blocking_reasons=[f'merge of part {number}: {exc}'])
        return False, None, []
    for cycle in cycles:
        cycle['subtask'] = number
        cycle['implementer'] = fixer
        cycle['merge'] = True
    if approved:
        execute(['git', 'add', '-A'], folder)
        if execute(['git', 'rev-parse', '-q', '--verify', 'MERGE_HEAD'], folder, check=False).returncode == 0:
            execute(['git', *git_identity(folder), 'commit', '--no-edit'], folder)
        else:
            commit_all(folder, f'ATS: integrate part {number}')
        record('subtask_merged', f'Part {number} merged after a reviewed resolution', task_id,
               subtask=number, implementer=fixer, reviewer=reviewer, branch=branch)
    return approved, reviewer, cycles


def subtask_flow(task, plan, task_id, folder, branch, implementer, unavailable, conf,
                 quiet=False, resumed=None):
    """Build a split task part by part; independent pairs are built and reviewed in parallel.

    A single part: one agent implements, the other reviews, and the reviewer builds the next part.
    A pair: both agents implement one part at the same time (the second in its own worktree), each
    reviews the other's part, then ATS merges the pair (see merge_parallel_part).
    """
    waves = plan_waves(plan)
    if waves is None:
        raise RuntimeError('Task plan must contain 2 to 20 non-empty parts (strings, or pairs of strings)')
    total = sum(len(w) for w in waves)
    history = []
    attribution = list((resumed or {}).get('attribution', []))
    completed = int((resumed or {}).get('completed', 0))
    reviewer, author, number = None, implementer, 0
    for wave in waves:
        if number + len(wave) <= completed:
            for skipped in range(number + 1, number + len(wave) + 1):
                record('subtask_resumed', 'Reusing independently reviewed code at a verified Git checkpoint',
                       task_id, subtask=skipped, branch=branch)
            number += len(wave)
            old = next((a for a in reversed(attribution) if a.get('subtask') == number), None)
            if old:
                reviewer, author = old.get('reviewer'), old.get('implementer')
                implementer = reviewer or implementer
            continue
        if number < completed:
            raise RuntimeError('Checkpoint splits a parallel wave; refusing unsafe resume')
        other = alternate_agent(implementer, unavailable)
        if implementer in unavailable or other is None:
            record('escalated', 'Both agents are required for subtask handoffs', task_id,
                   subtask=number + 1, branch=branch)
            return False, reviewer, implementer, history
        if len(wave) == 1:
            number += 1
            approved, reviewer, author, cycles = run_part(
                task, plan, number, total, wave[0], task_id, folder, branch, implementer, unavailable, conf,
                base=(execute(['git', 'rev-parse', 'HEAD^{tree}'], folder).stdout.strip()
                      if resumed else index_tree(folder)), quiet=quiet, resume_work=bool(resumed))
            history.extend(cycles)
            if not approved:
                return False, reviewer, author, history
            attribution.append(dict(subtask=number, implementer=author, reviewer=reviewer))
            commit_all(folder, f'ATS: reviewed part {number}')
            checkpoints.save_checkpoint(DATA, task_id, plan, folder,
                                        execute(['git', 'rev-parse', 'HEAD'], folder).stdout.strip(),
                                        number, attribution)
            implementer = reviewer or other
            continue

        # ---- a pair, in parallel: part `first` here, part `second` in a side worktree
        first, second = number + 1, number + 2
        number += 2
        commit_all(folder, f'ATS: reviewed parts 1-{first - 1}' if first > 1 else 'ATS: start of parallel parts')
        wave_base = execute(['git', 'rev-parse', 'HEAD'], folder).stdout.strip()
        side = folder.parent / f'{folder.name}-part{second}'
        side_branch = f'{branch}-part{second}'
        if side.exists():
            raise RuntimeError('Parallel side worktree already exists; inspect it before resuming: ' + str(side))
        execute(['git', 'worktree', 'add', '-b', side_branch, str(side), wave_base], folder)
        phase('parallel', task_id, subtasks=[first, second], implementers=[implementer, other])
        results = {}

        def build(key, *args):
            try:
                results[key] = run_part(*args)
            except Exception as exc:   # never leave the other thread waiting on a crash
                record('escalated', str(exc), task_id, subtask=args[2], branch=branch,
                       blocking_reasons=[f'subtask {args[2]}: {exc}'])
                results[key] = (False, None, args[8], [])

        threads = [
            threading.Thread(target=build, args=('a', task, plan, first, total, wave[0], task_id, folder, branch,
                                                 implementer, unavailable, conf, wave_base, quiet,
                                                 (second, wave[1]))),
            threading.Thread(target=build, args=('b', task, plan, second, total, wave[1], task_id, side, branch,
                                                 other, unavailable, conf, wave_base, quiet,
                                                 (first, wave[0]))),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        (ok_a, reviewer_a, author_a, cycles_a), (ok_b, reviewer_b, author_b, cycles_b) = results['a'], results['b']
        history.extend(cycles_a + cycles_b)
        if not (ok_a and ok_b):
            print(f'ATS: parallel parts {first}/{second} not both approved; side worktree kept: {side}',
                  file=sys.stderr)
            return False, reviewer_a or reviewer_b, author_a, history
        commit_all(folder, f'ATS: part {first}')
        commit_all(side, f'ATS: part {second}')
        attribution.append(dict(subtask=first, implementer=author_a, reviewer=reviewer_a))
        attribution.append(dict(subtask=second, implementer=author_b, reviewer=reviewer_b))
        merged, merge_reviewer, merge_cycles = merge_parallel_part(
            task, plan, second, wave[1], task_id, folder, branch, side_branch, author_b, unavailable, conf,
            quiet=quiet)
        history.extend(merge_cycles)
        if not merged:
            print(f'ATS: merging part {second} needs a human decision; side worktree kept: {side}',
                  file=sys.stderr)
            return False, merge_reviewer, author_b, history
        execute(['git', 'worktree', 'remove', '--force', str(side)], folder, check=False)
        execute(['git', 'branch', '-D', side_branch], folder, check=False)
        checkpoints.save_checkpoint(DATA, task_id, plan, folder,
                                    execute(['git', 'rev-parse', 'HEAD'], folder).stdout.strip(),
                                    number, attribution)
        reviewer, author = reviewer_b, author_b
        implementer = other   # the next wave starts with the other agent
    record('subtasks_completed', 'All parts independently reviewed; combined diff has not been reviewed',
           task_id, parts=attribution, branch=branch, combined_review=False)
    return True, reviewer, author, history


def distill_learnings(task, task_id, folder, history, preferred, unavailable, quiet=False):
    """After the reviews: turn the findings into reusable learnings for future tasks.

    One short, read-only agent call (the reviewer, else the other agent). Low-risk learnings are
    approved automatically; drastic ones wait for a human. Never fails the task.
    """
    findings = []
    for cycle in history:
        answers = {r.get('id'): r for r in cycle.get('responses') or []}
        for finding in cycle.get('findings') or []:
            answer = answers.get(finding.get('id')) or {}
            findings.append({k: v for k, v in {
                'id': finding.get('id'), 'severity': finding.get('severity'),
                'where': f"{finding.get('file') or ''}:{finding.get('line') or ''}".strip(':'),
                'summary': finding.get('summary'), 'cycle': cycle.get('cycle'), 'part': cycle.get('subtask'),
                'answer': answer.get('status'), 'note': answer.get('note')}.items() if v not in (None, '')})
    if not findings:
        return []
    known = sorted(known_titles())[:60]
    prompt = (f'Read {ATS/"POLICY.md"}. You are distilling lessons from a finished ATS task. Do NOT change any '
              f'files.\nTask: {task}\nReview findings and the implementer\'s answers: '
              + json.dumps(findings[:40], ensure_ascii=False) +
              '\nAlready known (do not repeat): ' + json.dumps(known, ensure_ascii=False) +
              '\nWrite at most 5 learnings that help FUTURE tasks avoid these problems: general and actionable, '
              'not a description of this change; skip one-off typos. kind: "lesson" (insight), "rule" (always/never '
              'do X) or "pattern". scope: "shared" when it applies to any project, else "project". risk: "high" only '
              'if following it could delete or overwrite data, rewrite git history, weaken review/tests/security, '
              'touch credentials or money, or do something irreversible; otherwise "low". evidence: file:line or '
              'finding id.\nEnd with ONE LINE: ATS_LEARNINGS_JSON: [{"kind":"lesson","title":"<title>",'
              '"observation":"<observation>","evidence":"<evidence>","scope":"project","risk":"low"}] '
              '(replace the placeholders, or use []).')
    tree = index_tree(folder)
    try:
        phase('learning', task_id, agent=preferred)
        agent, output = invoke_with_fallback(preferred, prompt, folder, unavailable, task_id, 'learning',
                                             quiet=quiet)
        saved = capture_learning_proposals(output.replace(prompt, ''), task_id)
    except Exception as exc:   # learning is a bonus; the task result stands
        print(f'ATS: distilling learnings skipped: {str(exc)[:200]}', file=sys.stderr)
        return []
    finally:
        changed = restore_if_changed(folder, tree)
        if changed:
            print('ATS: the learning step changed files; reverted: ' + ', '.join(changed[:10]), file=sys.stderr)
    waiting = [item for item in saved if item['status'] == 'proposed']
    if saved:
        record('learnings', f'{len(saved)} learnings distilled ({len(waiting)} need a human)', task_id,
               learnings=[{'id': i['id'], 'title': i['title'], 'status': i['status'],
                           'needs_human': i.get('needs_human')} for i in saved])
        print(f'ATS: {len(saved)} learnings stored; {len(waiting)} need your approval.', flush=True)
    return saved


def preset_task_id():
    """ATS_TASK_ID lets a caller know the task id before the run starts."""
    value = os.environ.get('ATS_TASK_ID', '').strip()
    if not value:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise RuntimeError('ATS_TASK_ID must be a UUID: ' + value)


def preview_task_plan(task, agent='auto', quiet=True):
    """Run ATS's actual preflight planner in a disposable read-only snapshot."""
    repository = execute(['git', 'rev-parse', '--show-toplevel'], check=False)
    local_mode = repository.returncode != 0
    if not local_mode and Path(repository.stdout.strip()).resolve() != APP.resolve():
        raise RuntimeError('Run ATS from the project repository root')
    selected = select(task) if agent == 'auto' else agent
    if selected not in enabled_agents():
        raise RuntimeError('Selected planning agent is disabled')
    unavailable = UnavailableAgents()
    task_id = str(uuid.uuid4())
    folder = APP.parent / f'{APP.name}-ats-plan-{task_id[:8]}'
    try:
        if local_mode:
            make_local_workspace(folder, 'ats/plan-' + task_id[:8])
        else:
            execute(['git', 'worktree', 'add', '--detach', str(folder), 'HEAD'])
        plan = preplan_task(task, task_id, folder, selected, unavailable, project(),
                            quiet=quiet, mode='auto' if should_preplan(task) else 'always')
        print('ATS_PLAN_JSON: ' + json.dumps(plan, ensure_ascii=False), flush=True)
        return plan
    finally:
        if local_mode:
            shutil.rmtree(folder, ignore_errors=True)
        else:
            execute(['git', 'worktree', 'remove', '--force', str(folder)], check=False)


def provided_task_plan(path, task):
    """Validate explicit user-edited plan from the app; never trust plan file paths as code."""
    source = Path(path).expanduser().resolve()
    if not source.is_file() or source.stat().st_size > 32768:
        raise RuntimeError('Plan file missing or too large')
    payload = json.loads(source.read_text(encoding='utf-8'))
    if not isinstance(payload, dict) or payload.get('task') != task:
        raise RuntimeError('Plan is not bound to the requested task')
    if payload.get('plan_version') == 2:
        try:
            graph = dag_planning.validate_dag(payload)
            return graph
        except ValueError as exc:
            raise RuntimeError('Invalid DAG task plan: ' + str(exc)) from exc
    plan = payload.get('waves')
    if not valid_plan(plan):
        raise RuntimeError('Invalid custom task plan (2-20 parts, up to 2 per wave)')
    return plan


def fresh_base(conf):
    """The ref a new task starts from: origin/<base_branch> right after a fetch.

    Starting from the local HEAD made work begin on an old main whenever the local checkout was
    behind GitHub, which caused most merge conflicts. Without a usable remote it is HEAD.
    """
    base = conf.get('base_branch', 'main')
    if 'origin' not in execute(['git', 'remote'], check=False).stdout.split():
        return 'HEAD'
    fetched = execute(['git', 'fetch', '--quiet', 'origin', base], check=False, timeout=120)
    ref = f'origin/{base}'
    if fetched.returncode == 0 and execute(['git', 'rev-parse', '--verify', '--quiet', ref], check=False).returncode == 0:
        return ref
    print(f'ATS: could not fetch {ref}; starting from the local HEAD.', file=sys.stderr)
    return 'HEAD'


def update_from_base(task, task_id, folder, branch, conf, implementer, unavailable, quiet=False):
    """Bring the latest base branch into the task branch before it is published.

    Already up to date: nothing to do. A clean merge whose checks pass: done. A conflict or failing
    checks: the implementer resolves it and the other agent reviews the whole change on top of the
    new base (same approval gate). Returns (ok, review cycles).
    """
    base = conf.get('base_branch', 'main')
    ref = f'origin/{base}'
    if execute(['git', 'fetch', '--quiet', 'origin', base], folder, check=False, timeout=120).returncode:
        print(f'ATS: could not fetch {ref}; publishing without updating the branch.', file=sys.stderr)
        return True, []
    if execute(['git', 'merge-base', '--is-ancestor', ref, 'HEAD'], folder, check=False).returncode == 0:
        return True, []
    phase('update', task_id, base=ref)
    merged = execute(['git', *git_identity(folder), 'merge', '--no-ff', '--no-edit', '-m', f'ATS: merge {ref}', ref],
                     folder, check=False)
    if merged.returncode == 0:
        checks = run_checks(conf, folder)
        if all(c['passed'] for c in checks):
            record('base_merged', f'{ref} merged cleanly into the task branch', task_id, branch=branch)
            print(f'ATS: branch updated with {ref}.', flush=True)
            return True, []
        problem = (f'After merging the latest {base} into your branch these checks fail; fix them so your change '
                   f'works on top of the new {base}:\n'
                   + json.dumps([{'command': c['command'], 'output': c['output'][-1500:]}
                                 for c in checks if not c['passed']], ensure_ascii=False))
    else:
        conflicts = execute(['git', 'diff', '--name-only', '--diff-filter=U'], folder, check=False).stdout.split()
        problem = (f'While you worked, {base} changed. Merging it into your branch produced conflicts in: '
                   + ', '.join(conflicts[:30]) + f'. Resolve every conflict marker: keep everything that is new on '
                   f'{base} and re-apply the intent of your change on top of it.')
    record('merge_conflict', problem[:1200], task_id, implementer=implementer, branch=branch, base=ref)
    fixer = implementer if implementer not in unavailable else ('claude' if implementer == 'codex' else 'codex')
    prompt = (f'Read {ATS/"POLICY.md"} and {ATS/"AGENTS.md"}. Task: {task}\n{problem}\n'
              'Change only what the update needs. Do not commit, push or modify .ats; ATS commits after the review.'
              + style_context(folder, conf))
    try:
        fixer, output = invoke_with_fallback(fixer, prompt, folder, unavailable, task_id, 'implementation',
                                             quiet=quiet)
    except (AgentUsageExhausted, RuntimeError) as exc:
        record('escalated', str(exc), task_id, branch=branch, blocking_reasons=[f'update with {ref}: {exc}'])
        return False, []
    record('implementation', output[-1600:], task_id, implementer=fixer, branch=branch)
    review_task = (f'{task}\n(Update: the branch was merged with the latest {base}. The diff is the whole change '
                   f'on top of the new {base}; check it is still correct, including the conflict resolution.)')
    try:
        approved, reviewer, fixer, cycles = review_loop(review_task, task_id, folder, branch, fixer, unavailable,
                                                        conf, quiet=quiet, base=ref)
    except RuntimeError as exc:
        record('escalated', str(exc), task_id, branch=branch, blocking_reasons=[f'update with {ref}: {exc}'])
        return False, []
    for cycle in cycles:
        cycle['update'] = ref
    if approved:
        execute(['git', 'add', '-A'], folder)
        if execute(['git', 'rev-parse', '-q', '--verify', 'MERGE_HEAD'], folder, check=False).returncode == 0:
            execute(['git', *git_identity(folder), 'commit', '--no-edit'], folder)
        else:
            commit_all(folder, f'ATS: update with {ref}')
        record('base_merged', f'{ref} merged after a reviewed resolution', task_id, implementer=fixer,
               reviewer=reviewer, branch=branch)
    return approved, cycles


def task_flow(task, agent, push, quiet=False, parallel='auto', approved_risk=False, plan_file=None,
              resume_from=None):
    conf=project()
    risk = intelligence.risk_profile(task)
    if risk['require_human_approval'] and not approved_risk:
        raise RuntimeError('High-risk task requires --approve-risk after reviewing: ' + json.dumps(risk))
    repository = execute(['git', 'rev-parse', '--show-toplevel'], check=False)
    local_mode = repository.returncode != 0
    if not local_mode and Path(repository.stdout.strip()).resolve() != APP.resolve():
        raise RuntimeError('Run ATS from the application repository root')
    if local_mode and push:
        raise RuntimeError('This project is not a Git repository. Run .ats task without --push; local changes will be exported as a patch.')
    if agent != 'auto' and agent not in enabled_agents():
        raise RuntimeError(f'{agent} is disabled. Use .ats agents --enable {agent}')
    agent=select(task) if agent=='auto' else agent
    task_id=preset_task_id() or str(uuid.uuid4())
    unavailable=UnavailableAgents()
    branch='ats/'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')+'-'+task_id[:8]
    folder=APP.parent / f'{APP.name}-ats-{task_id[:8]}'
    resumed = None
    if resume_from:
        if local_mode or not plan_file:
            raise RuntimeError('Checkpoint resume needs a Git project and original plan file')
        try:
            source_id = str(uuid.UUID(resume_from))
        except ValueError as exc:
            raise RuntimeError('Invalid resume source ID') from exc
        plan_for_resume = provided_task_plan(plan_file, task)
        source = checkpoints.read_checkpoint(DATA, source_id)
        if not source:
            raise RuntimeError('No reviewed checkpoint available for this source run')
        folder = Path(source.get('worktree', '')).resolve()
        if (folder.parent != APP.parent.resolve() or
                not re.fullmatch(re.escape(APP.name) + r'-ats-[0-9a-f]{8}', folder.name) or
                not folder.is_dir()):
            raise RuntimeError('Checkpoint worktree does not match original project')
        branch = execute(['git', 'symbolic-ref', '--quiet', '--short', 'HEAD'], folder).stdout.strip()
        if not branch.startswith('ats/'):
            raise RuntimeError('Resumed branch is not managed by ATS')
        if execute(['git', 'rev-parse', '--show-toplevel'], folder).stdout.strip() != str(folder):
            raise RuntimeError('Resumed folder is not a Git worktree')
        head = execute(['git', 'rev-parse', 'HEAD'], folder).stdout.strip()
        resumed = checkpoints.verified_resume(DATA, source_id, plan_for_resume, folder, head)
        if not resumed:
            raise RuntimeError('Source checkpoint is stale or invalid. Existing code was not modified')
    record('assigned',task,task_id,implementer=agent,branch=branch,task_categories=intelligence.categories(task))
    record('risk_profile', 'Risk-based execution profile', task_id, **risk)
    if not any(executable(name) for name in enabled_agents()):
        raise RuntimeError('No enabled agent CLI found; run .ats agents and install an enabled CLI')
    worktree_created=False
    try:
        if resumed:
            record('resumed', 'Restored verified reviewed checkpoint', task_id,
                   source_run=resume_from, completed=resumed['completed'],
                   completed_nodes=resumed.get('completed_nodes', []), branch=branch)
            print(f'ATS: resuming {resumed["completed"]} reviewed parts on {branch}.', flush=True)
        elif local_mode:
            make_local_workspace(folder, branch)
            print('ATS: project has no Git repository; using an isolated local snapshot.', flush=True)
        else:
            start = fresh_base(conf)
            execute(['git','worktree','add','-b',branch,str(folder),start])
            print(f'ATS: task branch starts from {start}.', flush=True)
        worktree_created=True
        base_commit = execute(['git', 'rev-parse', 'HEAD'], folder).stdout.strip()
        if plan_file:
            preflight_plan = provided_task_plan(plan_file, task)
            record('task_split', 'Human-approved editable plan', task_id, subtasks=preflight_plan,
                   source='app_plan')
            phase('planned', task_id, waves=len(plan_waves(preflight_plan)),
                  parallel_pairs=sum(len(w) == 2 for w in plan_waves(preflight_plan)))
        else:
            preflight_plan = preplan_task(task, task_id, folder, agent, unavailable, conf,
                                         quiet=quiet, mode=parallel)
        phase('implementation', task_id, implementer=agent, branch=branch, worktree=str(folder))
        prompt = (f"Read {ATS/'POLICY.md'} and {ATS/'AGENTS.md'}. Task: {task}. "
                  "Assess task size before editing. If it is too large for one complete review "
                  f"(maximum diff {conf.get('max_review_diff_chars', 150000)} characters), "
                  "make NO file changes and return ONE LINE ATS_SUBTASKS_JSON: "
                  '["first bounded implementation with tests", "next bounded implementation with tests"]. '
                  "Use 2 to 20 concrete ordered parts that together complete the task; "
                  "each part must be independently testable and reviewable. "
                  "Two parts that do not depend on each other and touch different code may be given as a "
                  'pair, e.g. ["shared model", ["API endpoint", "settings screen"], "end-to-end test"]: '
                  "the two agents then build that pair at the same time and review each other's part. "
                  "Other parts run in order, alternating implementer and reviewer. "
                  "For a small task implement it normally without that marker. "
                  "Change only necessary application files and add tests. "
                  "Do not commit, push, merge, deploy, trade, expose secrets or modify .ats; "
                  "ATS commits your work after the review."
                  + style_context(folder, conf)
                  + knowledge_context()
                  + '\nAt the end include ONE LINE ATS_LEARNINGS_JSON: '
                  + '[{"kind":"finding","title":"<title>","observation":"<observation>",'
                  + '"evidence":"<evidence>"}] (replace the placeholders, or use []). '
                  + 'Only evidence-backed items, no secrets; proposals are not approved rules.')
        if preflight_plan is not None:
            plan = preflight_plan
            implementer = agent
        else:
            initial_tree = index_tree(folder)
            implementer,result=invoke_with_fallback(
                agent,prompt,folder,unavailable,task_id,'implementation',quiet=quiet)
            record('implementation',result[-1600:],task_id,implementer=implementer,branch=branch)
            capture_learning_proposals(result,task_id)
            plan = subtask_plan(result.replace(prompt, ''))
            if plan is not None and index_tree(folder) != initial_tree:
                raise RuntimeError('Task planning modified files; worktree retained for diagnosis')
        if plan is not None:
            if preflight_plan is None:
                record('task_split', 'Task divided into reviewable parts', task_id, subtasks=plan)
            approved,reviewer,implementer,history=subtask_flow(
                task,plan,task_id,folder,branch,implementer,unavailable,conf,quiet=quiet,resumed=resumed)
        else:
            approved,reviewer,implementer,history=review_loop(task,task_id,folder,branch,implementer,unavailable,conf,quiet=quiet,
                                                               base=base_commit)
        learning_agent = reviewer or alternate_agent(implementer) or implementer
        distill_learnings(task, task_id, folder, history,
                          learning_agent, unavailable, quiet=quiet)
        # Never commit edits that have not passed independent review.
        if approved:
            execute(['git','add','-A'],folder)
        staged = approved and execute(['git', 'diff', '--cached', '--quiet'], folder, check=False).returncode != 0
        if not staged:
            pass   # the agent already committed everything itself; nothing left to commit
        elif local_mode:
            execute(['git', '-c', 'user.name=ATS', '-c', 'user.email=ats@localhost',
                     'commit', '-m', 'ATS: '+task[:65]], folder)
        else:
            execute(['git','commit','-m','ATS: '+task[:65]],folder)
        print(f'Branch: {branch} | approved: {approved} | workspace: {folder}')
        phase('finished', task_id, approved=approved, branch=branch, worktree=str(folder))
        if local_mode:
            patch = export_local_patch(folder)
            if patch:
                phase('patch', task_id, patch=str(patch))
                print(f'Local patch: {patch}')
                print('Review and apply from your application directory: git apply --check "' + str(patch) + '"')
                print('Then: git apply "' + str(patch) + '"')
            else:
                print('No local patch generated.')
            print('Original project unchanged. GitHub PR unavailable without a project remote.')
            return
        if not approved:
            print('Independent review/checks incomplete: PR publishing blocked; worktree preserved')
            return
        parts = {}
        for c in history:   # the approving (last) cycle of every part
            if 'subtask' in c:
                parts[c['subtask']] = f"Part {c['subtask']}: implementer {c['implementer']}, reviewer {c['reviewer']}"
        attribution = ('\n'.join(parts[n] for n in sorted(parts))
                       + '\nEvery part was reviewed on its own diff; the combined change was not reviewed as a whole.'
                       if parts else '')
        if push:
            updated, update_cycles = update_from_base(task, task_id, folder, branch, conf, implementer,
                                                      unavailable, quiet=quiet)
            history.extend(update_cycles)
            if not updated:
                message = (f"Not published: the branch could not be updated with the latest "
                           f"{conf.get('base_branch', 'main')} without a human decision; worktree preserved")
                record('escalated', message, task_id, branch=branch, blocking_reasons=[message])
                print('ATS: ' + message, file=sys.stderr)
                return
            phase('pr', task_id, branch=branch)
            execute(['git','push','-u','origin',branch],folder)
            if not shutil.which('gh'):
                print('gh missing: branch pushed but draft PR must be created manually');return
            base=conf.get('base_branch','main')
            outcome=execute(['gh','pr','create','--draft','--base',base,'--head',branch,'--title','ATS: '+task[:65],
                             '--body',f'Task ID: {task_id}\nImplementer: {implementer}\nReviewer: {reviewer}\nReview cycles: {len(history)}\n{attribution}\nReview/check outcomes in ATS data. Human merge required.'],folder)
            print(outcome.stdout)
            phase('pr_created', task_id, url=outcome.stdout.strip()[-300:])
    except AgentUsageExhausted as exc:
        message = str(exc) if isinstance(exc, BothAgentsExhausted) else 'Usage voor beide agents is op'
        print(message, file=sys.stderr)
        record('blocked', message, task_id, branch=branch, resets=exc.resets)
        if worktree_created:
            print('Worktree retained: '+str(folder),file=sys.stderr)
    except Exception as e:
        record('error',str(e)[:2000],task_id,implementer=agent,branch=branch)
        if worktree_created:
            print('Worktree retained: '+str(folder),file=sys.stderr)
        raise

def child_run(argv):
    if not argv: raise RuntimeError('Usage: ats run -- command [args]')
    p=subprocess.Popen(argv,cwd=APP,start_new_session=(os.name!='nt'))
    def forward(sig, _frame):
        try:
            if os.name!='nt': os.killpg(p.pid,sig)
            else: p.send_signal(sig)
        except ProcessLookupError: pass
    original={}
    for sig in (signal.SIGINT,signal.SIGTERM):
        original[sig]=signal.getsignal(sig);signal.signal(sig,forward)
    try: return p.wait()
    finally:
        for sig,handler in original.items():signal.signal(sig,handler)

def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('task');p.add_argument('description',nargs='+');p.add_argument('--agent',choices=['auto', *SUPPORTED_AGENTS],default='auto');p.add_argument('--push',action='store_true');p.add_argument('--quiet',action='store_true',help='Hide live agent output and progress messages');p.add_argument('--parallel',choices=['auto','always','never'],default='auto',help='Pre-plan and parallelize independent subtasks');p.add_argument('--approve-risk',action='store_true');p.add_argument('--plan-file');p.add_argument('--resume-from')
    p=sub.add_parser('plan');p.add_argument('description',nargs='+');p.add_argument('--agent',choices=['auto', *SUPPORTED_AGENTS],default='auto')
    p=sub.add_parser('risk');p.add_argument('description',nargs='+')
    sub.add_parser('performance')
    p=sub.add_parser('feedback');p.add_argument('comments_json');p.add_argument('--existing-ids')
    p=sub.add_parser('knowledge-check');p.add_argument('items_json');p.add_argument('relations_json')
    p=sub.add_parser('event');p.add_argument('kind');p.add_argument('summary');p.add_argument('--task')
    p=sub.add_parser('run');p.add_argument('argv',nargs=argparse.REMAINDER)
    p=sub.add_parser('agents', help='List, enable or disable coding agents')
    p.add_argument('--enable', choices=SUPPORTED_AGENTS, action='append', default=[])
    p.add_argument('--disable', choices=SUPPORTED_AGENTS, action='append', default=[])
    sub.add_parser('sync');sub.add_parser('doctor');sub.add_parser('install');sub.add_parser('data-sync')
    p=sub.add_parser('learn');p.add_argument('kind',choices=sorted(KNOWLEDGE_KINDS));p.add_argument('title');p.add_argument('observation');p.add_argument('--evidence',required=True);p.add_argument('--task');p.add_argument('--confidence',choices=CONFIDENCE_LEVELS,default='medium')
    args=parser.parse_args()
    if os.environ.get('ATS_APP') and not APP.is_dir():
        raise RuntimeError('ATS_APP is not a directory: ' + str(APP))
    if os.environ.get('ATS_NO_SELF_UPDATE', '').strip() not in ('', '0', 'false'):
        pass   # the caller (e.g. the ATS app host) manages ATS source updates
    else:
        sync_in()
    exitcode=0
    if args.command == 'task':
        try:
            refresh_private_knowledge()
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            print('ATS: approved knowledge refresh unavailable: ' + str(exc),
                  file=sys.stderr)
    try:
        if args.command=='install':
            install_requirements()
            install_agents()
        elif args.command=='agents':
            selected = configure_agents(args.enable, args.disable) if args.enable or args.disable else enabled_agents()
            print(json.dumps({'enabled': selected, 'supported': list(SUPPORTED_AGENTS),
                              'installed': {name: bool(executable(name)) for name in SUPPORTED_AGENTS}},
                             indent=2))
        elif args.command=='risk':
            print(json.dumps(intelligence.risk_profile(' '.join(args.description)),indent=2))
        elif args.command=='performance':
            directory = DATA / cleanname(project().get('project', APP.name)) / 'events'
            print(json.dumps(intelligence.performance(intelligence.read_events(directory)),indent=2))
        elif args.command=='feedback':
            comments = json.loads(Path(args.comments_json).read_text(encoding='utf-8'))
            ids = json.loads(Path(args.existing_ids).read_text(encoding='utf-8')) if args.existing_ids else []
            print(json.dumps(intelligence.pr_feedback_items(comments, ids),indent=2))
        elif args.command=='knowledge-check':
            items = json.loads(Path(args.items_json).read_text(encoding='utf-8'))
            relations = json.loads(Path(args.relations_json).read_text(encoding='utf-8'))
            print(json.dumps(intelligence.relation_issues(items, relations),indent=2))
        elif args.command=='task':task_flow(' '.join(args.description),args.agent,args.push,args.quiet,args.parallel,args.approve_risk,args.plan_file,args.resume_from)
        elif args.command=='plan':preview_task_plan(' '.join(args.description), args.agent)
        elif args.command=='event':record(args.kind,args.summary,args.task)
        elif args.command=='data-sync':sync_private_data()
        elif args.command=='learn':learn(args.kind,args.title,args.observation,args.evidence,args.task,args.confidence,'human')
        elif args.command=='run':
            argv=args.argv[1:] if args.argv and args.argv[0]=='--' else args.argv
            record('program_started',' '.join(argv)[:500]);exitcode=child_run(argv)
            record('program_stopped',f'exit code {exitcode}',exit_code=exitcode)
        elif args.command=='doctor':
            print(json.dumps({'app':str(APP),'ats':str(ATS),'cli':{x:bool(executable(x)) for x in ('git',*SUPPORTED_AGENTS,'gh')},'enabled_agents':enabled_agents(),'project_config':project()},indent=2))
        else: print('ATS synchronized')
    finally:
        if args.command == 'task':
            auto_sync_private_data()
        try: sync_out()
        except Exception as exc:
            print('ATS sync-out failed; events kept locally, retry at next start: '+str(exc),file=sys.stderr)
            if exitcode==0:exitcode=2
    return exitcode

if __name__=='__main__':
    try:sys.exit(main())
    except (RuntimeError,subprocess.TimeoutExpired) as exc:
        print('ATS error: '+str(exc),file=sys.stderr);sys.exit(1)
