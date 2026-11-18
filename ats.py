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
from ats_modules import budget as budgets
from ats_modules import style_guard
from ats_modules import context_pack
from ats_modules import gate as pre_gate
from ats_modules import knowledge_select
from ats_modules import lenient_json
from ats_modules import plan_learning
from ats_modules import review_scope
from ats_modules import review_handoff
from ats_modules import write_scope
from ats_modules import merge_guard
from ats_modules import cli_fixture
from ats_modules import run_report
from ats_modules import severity as severity_floor
from ats_modules import findings_ledger
from ats_modules import screen_map
from ats_modules import snapshots
from ats_modules import ui_preview
from ats_modules import style_check
from ats_modules import test_env
from ats_modules import usage as usage_meter

from ats_contract import accepted_event, valid_phase

if __name__ == '__main__':
    sys.modules.setdefault('ats', sys.modules[__name__])   # moved modules `import ats`: same module

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
    if value == 'none':   # explicitly no private data (tests run by ATS, see isolated_env)
        return None
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
        # A detached checkout is a pinned version (e.g. the ATS-app submodule): whoever pinned it
        # manages updates, so run this version as-is instead of refusing every command.
        print('ATS: pinned checkout (detached HEAD); self-update skipped.', file=sys.stderr)
        return
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
RUN_PROGRESS_LOCK = threading.Lock()
_run_progress = {}   # task id -> number of persisted review_result events, including parallel parts


_stream_seq = 0


def emit(item):
    """Append one event as a JSON line to ATS_EVENTS_FILE for a live consumer.

    Best effort: a missing variable or an unwritable file never affects the task.
    """
    path = os.environ.get('ATS_EVENTS_FILE')
    if not path:
        return
    global _stream_seq
    try:
        with EMIT_LOCK, open(path, 'a', encoding='utf-8') as handle:   # parallel parts emit from two threads
            # A number per writer, so the consumer can tell a lost or repeated line from a quiet run.
            _stream_seq += 1
            line = json.dumps(dict(item, stream_seq=_stream_seq, stream_src=str(os.getpid())),
                              ensure_ascii=False, default=str) + '\n'
            handle.write(line)
            handle.flush()
    except OSError:
        pass


def phase(stage, task_id, **fields):
    """Stage marker: live for the app (event_type phase) and stored as a phase_changed event file, so
    a host that restarts can rebuild where every run was from the event files alone."""
    if not valid_phase(stage):
        raise ValueError('Unknown ATS event phase: ' + str(stage))
    # Agent calls in this thread are metered against this task, part and review round.
    usage_meter.set_context(task_id=task_id, subtask=fields.get('subtask'), cycle=fields.get('cycle'))
    emit(dict(timestamp=dt.datetime.now(dt.timezone.utc).isoformat(), task_id=task_id,
              event_type='phase', stage=stage, **fields))
    if os.environ.get('ATS_PERSIST_PHASES', '1') != '0':
        plain = {k: v if isinstance(v, (str, int, float, bool, type(None))) else str(v)
                 for k, v in fields.items() if k not in ('stage', 'live')}
        try:
            record('phase_changed', stage, task_id, stage=stage, live=False, **plain)
        except (OSError, ValueError, RuntimeError):
            pass   # the live marker went out; a failing write never stops the task


def record(kind, summary, task=None, live=True, **fields):
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
    if kind == 'review_result':
        with RUN_PROGRESS_LOCK:
            if task in _run_progress:
                _run_progress[task] += 1
    if live:
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


def knowledge_context(limit=20, task='', paths=(), budget=None):
    """Use verified and approved knowledge; never treat unverified proposals as policy.

    Safety rules always go along; other learnings are chosen by relevance to the task and files
    within a character budget (knowledge_select).
    """
    return vetted_knowledge(limit, task, paths, budget)[1]


def vetted_knowledge(limit=20, task='', paths=(), budget=None):
    """(chosen items, prompt text)."""
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
    records = [r for r in records if isinstance(r.get('title'), str) and isinstance(r.get('observation'), str)
               and isinstance(r.get('evidence'), list)]
    if budget is None:
        budget = int(project().get('knowledge_budget', knowledge_select.DEFAULT_BUDGET))
    return knowledge_select.select(records, task, paths, budget=budget, limit=limit)


_task_knowledge = {}


def task_knowledge(task_id, task='', paths=()):
    """The learnings for a whole task, chosen once so every prompt of the task starts identically
    (prompt caching). Records which learnings were given to the agents."""
    if task_id in _task_knowledge:
        return _task_knowledge[task_id]
    text = knowledge_context(task=task, paths=paths)
    try:
        chosen = [item for item in vetted_knowledge(task=task, paths=paths)[0]] if text else []
    except (OSError, ValueError, RuntimeError):
        chosen = []
    if chosen and task_id:
        record('knowledge_selected', f'{len(chosen)} vetted learnings given to the agents', task_id,
               knowledge=[{'ref': knowledge_select.ref(item), 'id': item.get('id'), 'kind': item.get('kind'),
                           'title': item.get('title', '')[:120]} for item in chosen])
    _task_knowledge[task_id] = text
    return text


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
        entries = lenient_json.loads(matches[-1])
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
    """Claude Code in print mode; without a prompt argument it reads the prompt from stdin.

    --add-dir lets Claude read POLICY.md and AGENTS.md: ATS lives outside the task worktree, and
    Claude Code otherwise denies reading files outside its working directory.
    """
    extra = []
    for directory in usage_meter.context().get('read_dirs') or ():
        extra += ['--add-dir', str(directory)]   # e.g. the review's screenshots, outside the worktree
    return agent_command('claude', '-p', '--permission-mode', 'acceptEdits', '--add-dir', str(ATS), *extra,
                         '--allowedTools', *claude_allowed_tools())


READ_ONLY_GIT = ('git status', 'git diff', 'git log', 'git show', 'git ls-files', 'git grep')


def claude_allowed_tools(conf=None):
    """Commands Claude may run without a prompt: the project's test commands and read-only git.

    In print mode nobody can answer a permission prompt, so without this Claude could not run the
    tests and delivered untested work. The tests run in the isolated environment ATS gives agents.
    """
    try:
        commands = (conf if conf is not None else project()).get('test_commands', []) or []
    except RuntimeError:
        commands = []
    allowed = []
    for command in commands:
        argv = shlex.split(command) if isinstance(command, str) else [str(p) for p in command]
        if argv:
            prefix = ' '.join(argv[:3] if len(argv) >= 3 and argv[1] == '-m' else argv[:1])
            allowed.append(f'Bash({prefix}:*)')
    pythons = ['python', 'python3']
    if _test_env is not None:
        pythons.insert(0, str(test_env.python_in(_test_env)))   # the exact interpreter ATS tells agents to use
    for python in pythons:
        for module in ('pytest', 'unittest', 'py_compile'):
            allowed.append(f'Bash({python} -m {module}:*)')
    if any(APP.glob('tests/*.cjs')) or any(APP.glob('tests/**/*.cjs')):
        allowed.append('Bash(node tests/:*)')   # the project's own frontend tests
    allowed += [f'Bash({c}:*)' for c in READ_ONLY_GIT]
    return list(dict.fromkeys(allowed))


_preflight_passed = set()   # (task, worktree) where Codex already proved it can read policy and write


def preflight_key(cwd, task_id=None):
    try:
        folder = str(Path(cwd).resolve())
    except OSError:
        folder = str(cwd)
    return (task_id, folder)


def codex_preflight_once(cwd, quiet=False, task_id=None):
    """The preflight runs once per worktree in a run; a sandbox block resets it (see invoke_with_fallback)."""
    key = preflight_key(cwd, task_id)
    if key in _preflight_passed:
        return False
    codex_preflight(cwd, quiet=quiet)
    _preflight_passed.add(key)
    return True


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


SESSIONS = usage_meter.Sessions()
IMPLEMENTER_PURPOSES = ('implementation', 'revision')


def worktree_tree(folder):
    """Tree hash of the worktree as it is, without touching its index (None when it cannot be made,
    e.g. during an unresolved merge)."""
    try:
        index = execute(['git', 'rev-parse', '--git-path', 'index'], folder, check=False).stdout.strip()
        tree = None
        # Windows can hold the temporary index open for a moment (virus scanner); a failed cleanup
        # must not turn a valid tree hash into None.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            copy = Path(temporary) / 'index'
            source = Path(folder) / index if index and not Path(index).is_absolute() else Path(index or '')
            # copy2 keeps the index mtime, so git still rehashes files changed in the same second
            # (racy entries); a fresh mtime would make the tree hash depend on timing.
            if index and source.is_file():
                shutil.copy2(source, copy)   # keep the index mtime, or git's racy-file check can miss a same-second edit
            env = dict(os.environ, GIT_INDEX_FILE=str(copy))
            for args in (['git', 'add', '-A'], ['git', 'write-tree']):
                result = subprocess.run(args, cwd=folder, env=env, capture_output=True, text=True, check=False)
                if result.returncode:
                    return None
            tree = result.stdout.strip() or None
        return tree
    except (OSError, subprocess.SubprocessError):
        return None


def session_settings():
    try:
        turns = int(os.environ.get('ATS_SESSION_MAX_TURNS', '4'))
        tokens = int(os.environ.get('ATS_SESSION_MAX_CONTEXT', '400000'))
    except ValueError:
        turns, tokens = 4, 400000
    return os.environ.get('ATS_SESSION_REUSE', '1') != '0', turns, tokens


def invoke_with_fallback(agent, prompt, cwd, unavailable, task_id, purpose, quiet=False,
                         fallback=True, resume_prompt=None):
    """Run `agent`, or another enabled agent when it is unavailable.

    `resume_prompt` is used only for implementation revisions and planning corrections.
    Reviews always start fresh. A session is resumed only for the same agent and unchanged worktree;
    on failure ATS retries with the complete `prompt` in a new session.
    """
    selected = enabled_agents()
    if agent not in selected:
        raise RuntimeError(f'Agent {agent} is disabled. Configure it with .ats agents --enable {agent}')
    candidates = [agent] + ([name for name in selected if name != agent] if fallback else [])
    reasons, resets = {}, []
    usage_meter.set_context(task_id=task_id, purpose=purpose)
    reuse, max_turns, max_context = session_settings()
    for candidate in candidates:
        key = (task_id, usage_meter.context().get('subtask'), candidate)
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
            if candidate == 'codex' and purpose == 'implementation' and preflight_key(cwd, task_id) not in _preflight_passed:
                if not quiet:
                    print('ATS: checking Codex policy access and sandbox write...', flush=True)
                codex_preflight_once(cwd, quiet=quiet, task_id=task_id)
            session = None
            if resume_prompt and reuse and candidate == agent and purpose in ('revision', 'planning'):
                session, why = SESSIONS.usable(key, worktree_tree(cwd), max_turns, max_context)
                if not session and why != 'no session':
                    record('session_handoff', 'New implementer session with a compact handoff: ' + why,
                           task_id, agent=candidate, subtask=key[1], reason=why)
            result = None
            usage_meter.remember_call(None)
            if session:
                try:
                    result = invoke(candidate, resume_prompt, cwd, quiet=quiet, resume=session)
                except SessionResumeFailed as exc:
                    SESSIONS.forget(key)
                    record('session_handoff', 'Could not continue the session; new session with handoff',
                           task_id, agent=candidate, subtask=key[1], reason=str(exc)[:300])
            if result is None:
                result = invoke(candidate, prompt, cwd, quiet=quiet,
                                **({'purpose': purpose} if candidate == 'antigravity' else {}))
            call = usage_meter.last_call()
            if (purpose in IMPLEMENTER_PURPOSES or purpose == 'planning') and call and call.get('agent') == candidate and call.get('session_id'):
                SESSIONS.remember(key, call['session_id'], worktree_tree(cwd), call['usage'])
            if candidate == 'codex' and sandbox_blocked(result):
                raise AgentSandboxBlocked('Codex reported a sandbox or policy block.')
            if not quiet:
                print(f'ATS: {candidate} finished {purpose}.', flush=True)
            return candidate, result
        except AgentSandboxBlocked as exc:
            _preflight_passed.discard(preflight_key(cwd, task_id))   # check again before the next Codex call here
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


_isolated_state = None


def isolated_env(cwd=None):
    """The environment for test commands and agent CLIs.

    Without ATS's own control variables (ATS_EVENTS_FILE, ATS_TASK_ID, ...): a project's tests, or
    a test an agent runs, must never write events into the live run. ATS's own state and private
    data point to a temporary folder (ATS_DATA_REPO=none), so tests of ATS itself can never touch
    the real ~/.ats-data.
    """
    global _isolated_state
    if _isolated_state is None:
        _isolated_state = tempfile.mkdtemp(prefix='ats-test-state-')
    # One state folder per worktree: the two parts of a parallel pair run their tests at the same time.
    # os.path, not Path: tests patch os.name, and Path then picks the wrong flavour.
    state = os.path.join(_isolated_state, hashlib.sha256(os.path.abspath(str(cwd or '.')).encode()).hexdigest()[:12])
    os.makedirs(state, exist_ok=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith('ATS_')}
    env.update(ATS_DATA_REPO='none', ATS_STATE_DIR=str(state))
    return test_env.environ(env, _test_env)


_test_env = None   # the project's virtualenv for test commands and agent CLIs (configure_test_env)


def configure_test_env(conf):
    """Use the project's virtualenv (python_env, .venv or venv) for every test command of this run."""
    global _test_env
    resolved = test_env.resolve(conf, APP)
    _test_env = resolved['env']
    return resolved


def environment_problem(conf, task_id):
    """Before any agent starts: can the test commands run with the project's requirements?
    Returns a blocking reason, or None."""
    if not conf.get('environment_check', True):
        return None
    base = {key: value for key, value in os.environ.items() if not key.startswith('ATS_')}
    report = test_env.check(conf, APP, base)
    tools = test_env.required_tools(conf, APP)
    missing_tools = test_env.missing_tools(tools, base, _test_env)
    if missing_tools:
        message = (f"Test environment incomplete: {', '.join(missing_tools)} not found on this computer, and the "
                   "project's tests need it (required_tools, or JavaScript tests found). Tests that skip themselves "
                   'without it would pass unnoticed. Install it, or set required_tools in .ats-project.json. Agents '
                   'never install programs; no agent was started for this.')
        report = dict(report, ok=False, message=(report['message'] + ' ' if report['message'] else '') + message)
    if report['python'] is None and report['ok'] and not tools:
        return None   # nothing to check: no Python test command or no requirement files
    record('test_environment', report['message'] or 'Test environment ready', task_id,
           ok=report['ok'], python=report['python'], env=report['env'], source=report['source'],
           missing=report['missing'], tools=tools, missing_tools=missing_tools)
    return None if report['ok'] else report['message']


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
                              shell=use_shell, env=isolated_env(cwd))
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


def stream_command(command, cwd, timeout=1800, quiet=False, stdin_text=None, render=None, activity=None,
                   agent=None):
    """Stream both process pipes while retaining full output for ATS checks.

    `stdin_text` (the agent prompt) is written to the process's stdin and then closed.
    `render` turns a stdout line into what is shown live (None hides it), e.g. JSON lines into text;
    the full raw output is returned either way.
    """
    process = subprocess.Popen(command, cwd=cwd, env=isolated_env(cwd), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True,
                               stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                               encoding='utf-8', errors='replace', bufsize=1)
    chunks = {'stdout': [], 'stderr': []}
    where = dict(usage_meter.context())   # the reader threads have no context of their own
    def reader(pipe, key, target):
        try:
            for line in iter(pipe.readline, ''):
                chunks[key].append(line)
                if activity and key == 'stdout' and where.get('task_id'):
                    step = activity(line)
                    if step:   # live only (agent_activity): what the agent is doing right now
                        emit(dict(timestamp=dt.datetime.now(dt.timezone.utc).isoformat(), task_id=where['task_id'],
                                  event_type='agent_activity', agent=agent, subtask=where.get('subtask'),
                                  purpose=where.get('purpose'), **step))
                if not quiet:
                    shown = render(line) if render and key == 'stdout' else line
                    if shown:
                        print(shown, end='', file=target, flush=True)
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


MODEL_PHASES = ('planning', 'implementation', 'revision', 'learning')   # never 'review'
_run_flags = {}   # task id -> {'high_risk': bool}


def selected_agent_model(agent, purpose=None):
    """The model for this agent and phase.

    ATS_MODEL_<AGENT>_<PHASE> may choose a model per phase (for example a lighter one for learnings).
    A review always uses the agent's main model (ATS_MODEL_<AGENT>), and so does every phase of a
    high-risk task.
    """
    names = ['ATS_MODEL_' + agent.upper()]
    task_id = usage_meter.context().get('task_id')
    if purpose in MODEL_PHASES and not _run_flags.get(task_id, {}).get('high_risk'):
        names.insert(0, f'ATS_MODEL_{agent.upper()}_{purpose.upper()}')
    for name in names:
        value = os.environ.get(name, '').strip()
        if not value:
            continue
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/:-]{0,127}', value):
            raise RuntimeError(f'Invalid configured model ID for {agent}')
        return value
    return None


STRUCTURED_AGENTS = ('claude', 'codex')
UNSUPPORTED_FLAG = re.compile(r"unexpected argument '--json'|unknown option '--output-format'|"
                              r"unknown option '--resume'|unrecognized subcommand 'resume'|"
                              r"unexpected argument 'resume'", re.IGNORECASE)
_structured_off = set()


class SessionResumeFailed(RuntimeError):
    """Continuing the implementer's earlier session did not work; ATS starts a fresh one."""


def structured_output(agent):
    """Read usage and session ids from the CLI's JSON output (ATS_STRUCTURED_OUTPUT=0 turns it off)."""
    return (agent in STRUCTURED_AGENTS and agent not in _structured_off
            and os.environ.get('ATS_STRUCTURED_OUTPUT', '1') != '0')


def agent_cli_command(agent, model=None, structured=False, resume=None):
    if agent == 'codex':
        command = codex_exec_command()[:-1]          # without the trailing '-' (prompt on stdin)
        if model:
            command += ['--model', model]
        if structured:
            command.append('--json')
        if resume:
            command += ['resume', resume]
        return command + ['-']
    commands = {
        'claude': claude_print_command,
        'gemini': lambda: agent_command('gemini', '-p', ''),
        'opencode': lambda: agent_command('opencode', 'run'),
    }
    command = commands[agent]()
    if model:
        command.extend(['--model', model])
    if agent == 'claude' and structured:
        # stream-json (opt-in, ATS_CLAUDE_STREAM=1) shows each step live; the final result object,
        # and so the parsed answer, is the same as with json.
        command += (['--output-format', 'stream-json', '--verbose'] if os.environ.get('ATS_CLAUDE_STREAM') == '1'
                    else ['--output-format', 'json'])
        if resume:
            command += ['--resume', resume]
    return command


def note_usage(agent, purpose, info):
    """Record the token usage of one call and count it against the part's budget."""
    current = usage_meter.context()
    budget = current.get('budget')
    if budget is not None:
        budget.add(info['usage'])
    task_id = current.get('task_id')
    if not task_id:
        return
    counts = info['usage']
    record('agent_usage', f"{agent} {purpose}", task_id, agent=agent, model=info.get('model'),
           purpose=purpose, subtask=current.get('subtask'), cycle=current.get('cycle'),
           session_resumed=bool(info.get('resumed')), billable_tokens=usage_meter.billable(counts), **counts)


def invoke(agent, prompt, cwd, quiet=False, backoff=None, purpose='implementation', resume=None):
    if agent == 'antigravity':
        return invoke_antigravity(prompt, cwd, quiet=quiet, backoff=backoff, purpose=purpose)
    if agent not in ('codex', 'claude', 'gemini', 'opencode'):
        raise RuntimeError(f'Unsupported agent: {agent}')
    purpose = usage_meter.context().get('purpose', purpose)
    structured = structured_output(agent)
    if resume and not structured:
        raise SessionResumeFailed(f'{agent} session resume needs structured output')
    model = selected_agent_model(agent, purpose)
    command = agent_cli_command(agent, model, structured, resume)
    if agent == 'opencode':
        command.append(prompt)
    if not executable(agent):
        raise RuntimeError(f'{agent} executable missing; run .ats install')
    delays = RATE_LIMIT_BACKOFF if backoff is None else backoff
    attempt = 0
    usage_meter.remember_call(None)
    while True:
        p = stream_command(command, cwd, timeout=1800, quiet=quiet,
                           stdin_text=None if agent == 'opencode' else prompt,
                           **({'render': usage_meter.RENDERERS[agent], 'activity': usage_meter.ACTIVITY.get(agent),
                               'agent': agent} if structured else {}))
        parsed = usage_meter.PARSERS[agent](p.stdout) if structured else None
        out = (parsed['text'] if parsed else (p.stdout or '')) + '\n' + (p.stderr or '')
        if structured and p.returncode and UNSUPPORTED_FLAG.search((p.stdout or '') + (p.stderr or '')):
            # An older CLI without JSON output or resume: fall back to plain text from now on.
            _structured_off.add(agent)
            print(f'ATS: {agent} has no JSON output; using plain text.', file=sys.stderr, flush=True)
            if resume:
                raise SessionResumeFailed(f'{agent} cannot resume sessions')
            structured = False
            command = agent_cli_command(agent, model)
            continue
        if p.returncode and is_transient_rate_limit(out) and attempt < len(delays):
            print(f'ATS: {agent} is rate limited; retrying in {delays[attempt]}s '
                  f'({attempt + 1}/{len(delays)}).', file=sys.stderr, flush=True)
            time.sleep(delays[attempt])
            attempt += 1
            continue
        break
    if parsed:
        info = {'agent': agent, 'model': model or parsed.get('model'), 'session_id': parsed.get('session_id'),
                'usage': parsed['usage'], 'resumed': bool(resume)}
        usage_meter.remember_call(info)
        note_usage(agent, purpose, info)
    if p.returncode:
        if sandbox_blocked(out):
            raise AgentSandboxBlocked(f'{agent} sandbox blocked: {out[-1200:]}')
        if is_usage_exhausted(out):
            raise AgentUsageExhausted(agent, usage_reset_hint(out))
        if resume:
            raise SessionResumeFailed(f'{agent} could not continue session: {out[-600:]}')
        raise RuntimeError(f'{agent} error: {out[-1800:]}')
    if sandbox_blocked(out):
        raise AgentSandboxBlocked(f'{agent} sandbox blocked: {out[-1200:]}')
    return out


def route(task):
    """(agent, scores, reasons): which enabled agent implements this task and why.

    Scores come from routing keywords in the task, the approvals of the last 100 review results and,
    with enough samples, the measured results per task category.
    """
    conf=project()
    scores={name: 0 for name in enabled_agents()}
    if not scores:
        raise RuntimeError('No available agents: check the host weekly usage limits.')
    reasons={name: [] for name in scores}
    keywords={'codex':['test','bug','python','fix'], 'claude':['architecture','design','refactor']}
    keywords.update(conf.get('routing',{}))
    for agent, words in keywords.items():
        hits=[x for x in words if x.lower() in task.lower()]
        if agent in scores and hits:
            scores[agent] += 2 * len(hits)
            reasons[agent].append(f"keywords {', '.join(hits[:5])}: +{2 * len(hits)}")
    events=sorted((DATA / cleanname(conf.get('project', APP.name)) / 'events').glob('*.json'))[-100:]
    history={name: 0 for name in scores}
    for file in events:
        try:
            e=read_json(file); agent=e.get('implementer')
            if agent in scores and e.get('event_type')=='review_result':
                history[agent] += 1 if e.get('approved') else -1
        except (ValueError,OSError): pass
    for agent, value in history.items():
        if value:
            scores[agent] += value
            reasons[agent].append(f'recent reviews: {value:+d}')
    metrics = intelligence.performance(intelligence.read_events(DATA / cleanname(conf.get('project', APP.name)) / 'events'))
    relevant = set(intelligence.categories(task))
    for row in metrics:
        if row['reliable'] and row['agent'] in scores and row['category'] in relevant:
            value = round(4 * row['approval_rate'] - min(3, row['revisions'] / row['samples']))
            scores[row['agent']] += value
            reasons[row['agent']].append(f"{row['category']} results ({row['samples']} runs): {value:+d}")
    default = conf.get('default_agent','codex')
    chosen = max(scores,key=lambda n:(scores[n],n==default))
    if len(set(scores.values())) == 1 and len(scores) > 1:
        reasons[chosen].append('equal scores: project default' if chosen == default else 'equal scores')
    return chosen, scores, reasons


def select(task):
    return route(task)[0]

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


_background = None


def background():
    """Threads for work that runs next to a review (the full tests)."""
    global _background
    if _background is None:
        import concurrent.futures
        _background = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix='ats-bg')
    return _background


SUBTASK_EXAMPLE = ["first bounded implementation with tests", "next bounded implementation with tests"]


MAX_PARTS = 20
MAX_PARALLEL_PARTS = 2   # one part per agent


def base_moved(start, expect_base):
    """A blocking reason when the base branch moved since the caller made its preflight or plan."""
    if not expect_base:
        return None
    if not re.fullmatch(r'[0-9a-f]{7,40}', str(expect_base)):
        return 'Invalid --expect-base: give the commit SHA the plan was made on'
    head = execute(['git', 'rev-parse', start], check=False).stdout.strip()
    if head.startswith(expect_base) or expect_base.startswith(head) and head:
        return None
    return (f'The base branch moved since the plan was made: it was {expect_base[:12]}, it is now {head[:12]}. '
            'Nothing was started. Check the plan against the new code (plan again), then start the run.')


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


def update_from_base(task, task_id, folder, branch, conf, implementer, unavailable, quiet=False, rules='',
                     guard=None):
    """Bring the latest base branch into the task branch before it is published.

    Already up to date: nothing to do. A clean merge whose checks pass: done. A conflict or failing
    checks: the implementer resolves it and the other agent reviews the whole change on top of the
    new base (same approval gate). Returns (ok, review cycles).

    `rules`: extra instructions for the agent that resolves (used when fixing a pull request).
    `guard`: called after the resolution, returns a note for the reviewer (see merge_guard).
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
    problem += rules
    record('merge_conflict', problem[:1200], task_id, implementer=implementer, branch=branch, base=ref,
           files=conflicts[:50] if merged.returncode else [])
    fixer = implementer if implementer not in unavailable else ('claude' if implementer == 'codex' else 'codex')
    prompt = (shared_prefix(folder, conf, task_id) + 'ROLE: implementer, updating the branch.\n'
              f'Task: {task}\n{problem}\n'
              'Change only what the update needs. Do not commit, push or modify .ats; ATS commits after the review.')
    try:
        fixer, output = invoke_with_fallback(fixer, prompt, folder, unavailable, task_id, 'implementation',
                                             quiet=quiet)
    except (AgentUsageExhausted, RuntimeError) as exc:
        record('escalated', str(exc), task_id, branch=branch, blocking_reasons=[f'update with {ref}: {exc}'])
        return False, []
    record('implementation', output[-1600:], task_id, implementer=fixer, branch=branch)
    guard_note = guard(folder) if guard else ''
    review_task = (f'{task}\n(Update: the branch was merged with the latest {base}. The diff is the whole change '
                   f'on top of the new {base}; check it is still correct, including the conflict resolution.)')
    try:
        approved, reviewer, fixer, cycles = review_loop(review_task, task_id, folder, branch, fixer, unavailable,
                                                        conf, quiet=quiet, base=ref, kind='update',
                                                        earlier_review_notes=guard_note)
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


def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('task');p.add_argument('description',nargs='+');p.add_argument('--agent',choices=['auto', *SUPPORTED_AGENTS],default='auto');p.add_argument('--push',action='store_true');p.add_argument('--quiet',action='store_true',help='Hide live agent output and progress messages');p.add_argument('--parallel',choices=['auto','always','never'],default='auto',help='Pre-plan and parallelize independent subtasks');p.add_argument('--approve-risk',action='store_true');p.add_argument('--plan-file');p.add_argument('--resume-from');p.add_argument('--recheck-from',help='Test and review the kept worktree of an earlier run again, without rebuilding');p.add_argument('--expect-base',help='Commit the plan was made on; the run stops when the base branch moved');p.add_argument('--fix-pr',type=int,help='Resolve the merge conflicts of this open pull request (needs --pr-head and --pr-base); with --push its branch is updated after review');p.add_argument('--pr-head');p.add_argument('--pr-base');p.add_argument('--pr-url')
    p=sub.add_parser('plan');p.add_argument('description',nargs='+');p.add_argument('--agent',choices=['auto', *SUPPORTED_AGENTS],default='auto')
    p=sub.add_parser('risk');p.add_argument('description',nargs='+')
    sub.add_parser('env-check', help='Check that the test commands find the project requirements (JSON)')
    p=sub.add_parser('screens', help='Show the screen map, a draft of it, the screens a task names, or the task that completes it (JSON)');p.add_argument('--match');p.add_argument('--task',action='store_true',help='Print the task text that lets agents complete .ats-screens.json through a normal reviewed run')
    p=sub.add_parser('clarify', help='One question with choices when a UI task does not say which screen (JSON; no agent)');p.add_argument('description',nargs='+')
    p=sub.add_parser('preview-check', help='Check that screenshot reviews work for this project (start command, screen map, Playwright) and take one screenshot');p.add_argument('--screen')
    p=sub.add_parser('main-check', help='Run the test commands on a clean checkout of origin/<base_branch> (local CI; changes nothing)');p.add_argument('--ref')
    p=sub.add_parser('agent-check', help='Run each installed agent CLI on a tiny fixture repo and check ATS markers (JSON)');p.add_argument('--agent',action='append')
    p.add_argument('--fixture', help='Also write an anonymous fixture (versions, OS, error categories) to this JSON file; with an existing file the differences are printed')
    sub.add_parser('performance')
    p=sub.add_parser('report', help='Summarize the run reports of the last days');p.add_argument('--days',type=positive_days,default=7)
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
        elif args.command=='clarify':
            print(json.dumps({'question': screen_map.clarify(screen_map.load(APP)[0], ' '.join(args.description))}, ensure_ascii=False))
        elif args.command=='preview-check':
            sys.exit(0 if preview_check(args.screen)['ready'] else 1)
        elif args.command=='main-check':
            outcome = main_check(args.ref)
            sys.exit(0 if outcome['passed'] in (True, None) else 1)
        elif args.command=='agent-check':
            report = agent_check(args.agent)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if args.fixture:
                target = Path(args.fixture)
                fixture = cli_fixture.build(report)
                if target.is_file():
                    try:
                        for line in cli_fixture.compare(read_json(target), fixture) or ['same outcome as the saved fixture']:
                            print('ATS fixture: ' + line, file=sys.stderr)
                    except (ValueError, OSError):
                        pass
                target.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
            return 0 if all(r['ok'] for r in report['agents'].values()) else 1
        elif args.command=='screens':
            print(json.dumps(screens_command(args.match, args.task), ensure_ascii=False, indent=2))
        elif args.command=='env-check':
            conf = project()
            base = {key: value for key, value in os.environ.items() if not key.startswith('ATS_')}
            print(json.dumps(test_env.check(conf, APP, base), ensure_ascii=False))
        elif args.command=='performance':
            directory = DATA / cleanname(project().get('project', APP.name)) / 'events'
            print(json.dumps(intelligence.performance(intelligence.read_events(directory)),indent=2))
        elif args.command=='report':
            name = cleanname(project().get('project', APP.name))
            print(run_report.table(run_report.recent(DATA, name, args.days), args.days))
        elif args.command=='feedback':
            comments = json.loads(Path(args.comments_json).read_text(encoding='utf-8'))
            ids = json.loads(Path(args.existing_ids).read_text(encoding='utf-8')) if args.existing_ids else []
            print(json.dumps(intelligence.pr_feedback_items(comments, ids),indent=2))
        elif args.command=='knowledge-check':
            items = json.loads(Path(args.items_json).read_text(encoding='utf-8'))
            relations = json.loads(Path(args.relations_json).read_text(encoding='utf-8'))
            print(json.dumps(intelligence.relation_issues(items, relations),indent=2))
        elif args.command=='task':task_flow(' '.join(args.description),args.agent,args.push,args.quiet,args.parallel,args.approve_risk,args.plan_file,args.resume_from,args.recheck_from,args.expect_base,pull_request_argument(args))
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

# Moved to modules (t67); re-exported so `ats.<name>` and patches keep working.
from ats_modules.review_flow import *  # noqa: E402,F401,F403
from ats_modules.part_flow import *  # noqa: E402,F401,F403
from ats_modules.task_flow import *  # noqa: E402,F401,F403


if __name__=='__main__':
    try:sys.exit(main())
    except (RuntimeError,subprocess.TimeoutExpired) as exc:
        print('ATS error: '+str(exc),file=sys.stderr);sys.exit(1)
