#!/usr/bin/env python3
"""ATS entry point, called from an application repository with .ats clone."""
import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import fnmatch
import hashlib
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

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


def _sync_private_data():
    if PRIVATE_DATA_REPO is None:
        raise RuntimeError('Clone your private ATS data repository into ~/.ats-data or set ATS_DATA_REPO.')
    root = PRIVATE_DATA_REPO
    gh = validate_private_remote(root)
    repo = data_remote_repo(root)
    current = execute(['git', 'branch', '--show-current'], root).stdout.strip()
    if current != 'main':
        raise RuntimeError(
            f'ATS-data is on {current!r}, not main. Resolve the pending data branch '
            'before synchronizing again. No local history was discarded.'
        )
    status = execute(['git', 'status', '--porcelain', '--untracked-files=all'], root).stdout
    if any(not line[3:].replace('\\', '/').startswith(('data/', 'knowledge/'))
           for line in status.splitlines()):
        raise RuntimeError('Unexpected changes in ATS-data; resolve them before syncing.')

    if not status.strip():
        execute(['git', 'pull', '--ff-only', 'origin', 'main'], root)
        print('ATS: private knowledge up to date.', flush=True)
        return

    branch = ('ats-data/sync-' +
              dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S') +
              '-' + uuid.uuid4().hex[:8])
    execute(['git', 'switch', '-c', branch], root)
    try:
        execute(['git', 'add', '--', 'data', 'knowledge'], root)
        staged = execute(['git', 'diff', '--cached', '--name-only'], root).stdout.strip()
        if not staged:
            execute(['git', 'switch', 'main'], root)
            return
        execute(['git', 'commit', '-m', 'ATS: propose private events and learned knowledge'], root)
        execute(['git', 'push', '-u', 'origin', branch], root)
        pr = execute([gh, 'pr', 'create', '--repo', repo,
                      '--base', 'main', '--head', branch,
                      '--title', 'ATS: synchronize private agent knowledge',
                      '--body', 'Review private events and proposed knowledge before merging. No automatic merge.'], root)
        print('ATS: private knowledge PR: ' + pr.stdout.strip(), flush=True)
    finally:
        # Do not strand the shared checkout on a feature branch after success.
        # If committing or publishing failed with uncommitted changes, leave it
        # untouched so the user can recover the exact data safely.
        if not execute(['git', 'status', '--porcelain'], root).stdout.strip():
            execute(['git', 'switch', 'main'], root)


def refresh_private_knowledge():
    """Pull approved knowledge before a task, but never overwrite pending local events."""
    if PRIVATE_DATA_REPO is None:
        return
    with data_repo_lock(PRIVATE_DATA_REPO):
        _refresh_private_knowledge()


def _refresh_private_knowledge():
    root = PRIVATE_DATA_REPO
    validate_private_remote(root)
    branch = execute(['git', 'branch', '--show-current'], root).stdout.strip()
    if branch != 'main':
        raise RuntimeError('ATS-data checkout must be on main before refreshing knowledge.')
    status = execute(['git', 'status', '--porcelain', '--untracked-files=all'], root).stdout.strip()
    if not status:
        execute(['git', 'pull', '--ff-only', 'origin', 'main'], root)
    else:
        print('ATS: local data pending review; approved knowledge refresh skipped.', file=sys.stderr)


def auto_sync_private_data():
    """Best-effort PR creation after tasks; never hide the application's result."""
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


def emit(item):
    """Append one event as a JSON line to ATS_EVENTS_FILE for a live consumer.

    Best effort: a missing variable or an unwritable file never affects the task.
    """
    path = os.environ.get('ATS_EVENTS_FILE')
    if not path:
        return
    try:
        line = json.dumps(item, ensure_ascii=False, default=str) + '\n'
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(line)
            handle.flush()
    except OSError:
        pass


def phase(stage, task_id, **fields):
    """Live-only stage marker (not stored as an event file)."""
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
    (target / f'{now.replace(":", "-")}-{uid}.json').write_text(
        json.dumps(item, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    emit(item)
    return item

KNOWLEDGE_KINDS = {'finding', 'feedback', 'lesson', 'rule', 'decision', 'pattern'}
CONFIDENCE_LEVELS = ('low', 'medium', 'high')
KNOWLEDGE_PRIORITY = {'rule': 0, 'decision': 1, 'lesson': 2, 'pattern': 3, 'feedback': 4, 'finding': 5}


def learn(kind, title, observation, evidence, task_id=None, confidence='medium', created_by='human'):
    """Append an evidence-linked proposal; learned rules are never auto-approved."""
    if kind not in KNOWLEDGE_KINDS:
        raise RuntimeError('Invalid learning kind: ' + kind)
    if not evidence.strip():
        raise RuntimeError('Learning requires a supporting evidence reference.')
    if confidence not in CONFIDENCE_LEVELS:
        raise RuntimeError('Invalid confidence: ' + str(confidence))
    conf = project()
    name = cleanname(conf.get('project', APP.name))
    if not name:
        raise RuntimeError('Project name is required.')
    uid = str(uuid.uuid4())
    filename = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uid + '.json'
    sections = {'finding': 'findings', 'feedback': 'feedback', 'lesson': 'lessons',
                'rule': 'rules', 'decision': 'decisions', 'pattern': 'patterns'}
    target = (PRIVATE_DATA_REPO / 'knowledge' if PRIVATE_DATA_REPO else
              state_directory().parent / 'knowledge') / 'projects' / name / sections[kind]
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    item = {'id': uid, 'project': name, 'kind': kind,
            'title': title, 'observation': observation, 'evidence': [evidence],
            'status': 'proposed', 'confidence': confidence,
            'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'created_by': created_by, 'task_id': task_id}
    (target / filename).write_text(json.dumps(item, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return item


def knowledge_context(limit=12):
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


def capture_learning_proposals(agent_output, task_id):
    """Ingest clearly marked structured learnings, never model-authored approved rules.

    Agent CLIs may echo the prompt (which contains a format example), so only
    the LAST marked line is used and placeholder values are rejected.
    """
    entries = parse_marked_json(agent_output, 'ATS_LEARNINGS_JSON')
    saved = []
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
        saved.append(learn(entry['kind'], title, observation, evidence, task_id,
                           created_by='agent'))
    return saved


class AgentUsageExhausted(RuntimeError):
    def __init__(self, agent, resets=None):
        self.agent = agent
        self.resets = resets
        message = f"usage voor {agent} is op"
        super().__init__(message + (f' ({resets})' if resets else ''))


class BothAgentsExhausted(AgentUsageExhausted):
    def __init__(self, resets=None):
        RuntimeError.__init__(self, 'Usage voor beide agents is op' +
                              (f' ({resets})' if resets else ''))
        self.agent = 'beide agents'
        self.resets = resets


# Hard limits: the subscription/plan quota is used up until a reset time.
USAGE_PATTERNS = (
    r"usage limit(?: reached| exceeded| exhausted)?",
    r"quota exceeded",
    r"insufficient_quota",
    r"out of (?:credits|usage)",
    r"credit balance is too low",
    r"you.ve (?:reached|hit) your (?:\w+ )?limit",
    r"(?:hit|reached) (?:your|the) (?:usage|rate|weekly|monthly|daily|session|5-hour) limit",
    r"(?:weekly|monthly|daily|session|\d+-hour) limit (?:reached|exceeded|hit)",
    r"limit\b[^\n]{0,40}\bresets?\b",
)

# Transient throttling: retry with backoff before treating it as exhausted.
TRANSIENT_PATTERNS = (
    r"rate limit(?: reached| exceeded)?",
    r"too many requests",
    r"\b429\b",
    r"overloaded",
    r"\b529\b",
)

RESET_PATTERN = re.compile(
    r"\b(resets?\s+(?:at\s+|in\s+)?[^\n.·|]{1,60}|try again (?:at|in)\s+[^\n.|]{1,60})",
    re.IGNORECASE)
RATE_LIMIT_BACKOFF = (30, 120)


def is_hard_usage_limit(output):
    return any(re.search(pattern, output, flags=re.IGNORECASE) for pattern in USAGE_PATTERNS)


def is_transient_rate_limit(output):
    return (not is_hard_usage_limit(output) and
            any(re.search(pattern, output, flags=re.IGNORECASE) for pattern in TRANSIENT_PATTERNS))


def is_usage_exhausted(output):
    return is_hard_usage_limit(output) or is_transient_rate_limit(output)


def usage_reset_hint(output):
    """Return the reset hint an agent CLI printed, e.g. 'resets 11am (Europe/Amsterdam)'."""
    match = RESET_PATTERN.search(output or '')
    return match.group(1).strip() if match else None


class AgentSandboxBlocked(RuntimeError):
    pass


SANDBOX_PATTERNS = (
    r'blocked by policy',
    r'rejected: blocked',
    r'sandbox: read-only',
    r'permission denied.*sandbox',
    r'cannot.*(?:read|write).*sandbox',
)


def sandbox_blocked(output):
    return any(re.search(pattern, output, re.IGNORECASE) for pattern in SANDBOX_PATTERNS)


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
    other = 'claude' if agent == 'codex' else 'codex'
    reasons = {}
    resets = []
    for candidate in ((agent, other) if fallback else (agent,)):
        if candidate in unavailable:
            reasons[candidate] = unavailable_reason(unavailable, candidate)
            continue
        try:
            if not quiet:
                print(f'ATS: {candidate} starting {purpose}...', flush=True)
            if candidate == 'codex' and purpose == 'implementation':
                if not quiet:
                    print('ATS: checking Codex policy access and sandbox write...', flush=True)
                codex_preflight(cwd, quiet=quiet)
            result = invoke(candidate, prompt, cwd, quiet=quiet)
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
    raise RuntimeError('No usable agent available: ' +
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
    if os.name == 'nt' and name in ('npm', 'codex', 'claude'):
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
    print('ATS: configuring Codex first, then Claude Code.', flush=True)
    if not executable('codex'):
        if executable('npm'):
            execute([executable('npm'), 'install', '-g', '@openai/codex'], capture=False)
        elif shutil.which('brew'):
            execute(['brew', 'install', '--cask', 'codex'], capture=False)
        else:
            raise RuntimeError('Install Node.js/npm or Homebrew before installing Codex')
    if not executable('codex'):
        raise RuntimeError('Codex installed but not on PATH; reopen terminal and retry')
    if execute(agent_command('codex', 'login', 'status'), check=False).returncode:
        execute(agent_command('codex', 'login'), capture=False)
    if execute(agent_command('codex', 'login', 'status'), check=False).returncode:
        raise RuntimeError('Codex login not completed')
    print('Codex ready. Moving to Claude Code.', flush=True)
    if not executable('claude'):
        if shutil.which('brew'):
            execute(['brew', 'install', '--cask', 'claude-code'], capture=False)
        elif executable('npm'):
            execute([executable('npm'), 'install', '-g', '@anthropic-ai/claude-code'], capture=False)
        else:
            raise RuntimeError('Install Homebrew or Node.js/npm before installing Claude Code')
    if not executable('claude'):
        raise RuntimeError('Claude Code installed but not on PATH; reopen terminal and retry')
    result = execute(agent_command('claude', 'auth', 'status'), check=False)
    if result.returncode or not claude_authenticated(result.stdout):
        execute(agent_command('claude', 'auth', 'login'), capture=False)
    result = execute(agent_command('claude', 'auth', 'status'), check=False)
    if result.returncode or not claude_authenticated(result.stdout):
        raise RuntimeError('Claude Code login not completed')
    print('Both agents installed and authenticated.', flush=True)


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


def invoke(agent, prompt, cwd, quiet=False, backoff=None):
    command = codex_exec_command() if agent == 'codex' else claude_print_command()
    if not executable(agent):
        raise RuntimeError(f'{agent} executable missing; run .ats install')
    delays = RATE_LIMIT_BACKOFF if backoff is None else backoff
    attempt = 0
    while True:
        p = stream_command(command, cwd, timeout=1800, quiet=quiet, stdin_text=prompt)
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
    scores={'codex':0,'claude':0}
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


def review_prompt(task, checks, diff, history):
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
            + previous +
            "Write, in this order:\n"
            "1. Findings in prose: severity (BLOCKER, MAJOR, MINOR, SUGGESTION), file:line, issue, why it matters, concrete fix.\n"
            "2. ONE LINE listing only findings that are still open, or [] if none, in this shape: "
            'ATS_FINDINGS_JSON: [{"id":"<id>","severity":"<severity>","file":"<path>","line":"<line>","summary":"<summary>"}]\n'
            "3. Optionally ONE LINE ATS_LEARNINGS_JSON with evidence-backed reusable lessons (no secrets).\n"
            "4. As the very last line, exactly one verdict line (APPROVE only when no BLOCKER/MAJOR is open), "
            "either VERDICT: APPROVE or VERDICT: CHANGES_REQUESTED.\n"
            'Checks: ' + json.dumps(checks, ensure_ascii=False) + '\nDiff:\n' + diff)


def revision_prompt(task, review, findings):
    return (f"Revise your implementation for task {task!r}. Never push, merge, deploy or modify .ats.\n"
            "Address the review below. Answer EVERY finding with status fixed (with test/file "
            "evidence), not_applicable (with evidence why) or deferred (with an issue/reference). "
            "Deferred BLOCKERs and unanswered BLOCKER/MAJOR findings prevent approval.\n"
            "End with ONE LINE in this shape: "
            'ATS_RESPONSES_JSON: [{"id":"<id>","status":"<status>","note":"<evidence>","reference":"<reference>"}]\n'
            'Open findings: ' + json.dumps(findings, ensure_ascii=False) +
            '\nReview:\n' + review[-12000:])


def review_loop(task, task_id, folder, branch, implementer, unavailable, conf, quiet=False, base='HEAD'):
    """Independent review with enforced read-only reviewer and POLICY feedback loop.

    Returns (approved, reviewer, implementer, history).
    """
    max_revisions = int(conf.get('max_revisions', 2))
    max_chars = int(conf.get('max_review_diff_chars', 150000))
    history = []
    reviewer = None
    reasons = ['not reviewed']
    for cycle in range(max_revisions + 1):
        phase('checks', task_id, cycle=cycle)
        checks = run_checks(conf, folder)
        execute(['git', 'add', '-A'], folder)
        # Against the commit the worktree started from, not HEAD: an agent that runs `git commit`
        # itself would otherwise make its own work invisible ("Agent made no changes").
        diff = execute(['git', 'diff', '--cached', base], folder).stdout
        if not diff.strip():
            record('blocked', 'Agent made no changes; worktree preserved for diagnosis', task_id,
                   implementer=implementer, branch=branch)
            raise RuntimeError('Agent made no changes; worktree preserved for diagnosis: ' + str(folder))
        preferred = 'claude' if implementer == 'codex' else 'codex'
        if preferred in unavailable:
            message = f'Independent review unavailable: {preferred} is not available ({unavailable_reason(unavailable, preferred)})'
            print(message, file=sys.stderr)
            record('review_skipped', message, task_id, implementer=implementer, reviewer=preferred)
            reasons = ['no independent reviewer available']
            break
        if len(diff) > max_chars:
            message = (f'Diff is {len(diff)} characters, above max_review_diff_chars={max_chars}; '
                       'refusing to review a truncated diff. Split the task or raise the limit.')
            print('ATS: ' + message, file=sys.stderr)
            record('review_skipped', message, task_id, implementer=implementer, reviewer=preferred)
            reasons = ['diff too large to review completely']
            break
        tree = index_tree(folder)
        phase('review', task_id, cycle=cycle, reviewer=preferred, implementer=implementer)
        try:
            reviewer, review = invoke_with_fallback(preferred, review_prompt(task, checks, diff, history),
                                                    folder, unavailable, task_id, 'review',
                                                    quiet=quiet, fallback=False)
        except AgentUsageExhausted as exc:
            print(str(exc) + ': independent review skipped', file=sys.stderr)
            record('review_skipped', str(exc), task_id, implementer=implementer,
                   reviewer=preferred, resets=exc.resets)
            reasons = ['reviewer usage exhausted']
            break
        except RuntimeError as exc:
            restore_if_changed(folder, tree)
            print(f'ATS: review by {preferred} failed: {exc}', file=sys.stderr)
            record('review_skipped', str(exc)[:1200], task_id, implementer=implementer, reviewer=preferred)
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
        record('review_result', review[-8000:], task_id, implementer=implementer, reviewer=reviewer,
               approved=approved, verdict=verdict, findings=findings, blocking_reasons=reasons,
               tampered_paths=tampered, tests=checks, cycle=cycle, branch=branch)
        history.append({'cycle': cycle, 'reviewer': reviewer, 'findings': findings, 'responses': []})
        if approved:
            phase('approved', task_id, cycle=cycle)
            return True, reviewer, implementer, history
        if cycle == max_revisions:
            break
        phase('revision', task_id, cycle=cycle, implementer=implementer)
        try:
            implementer, response = invoke_with_fallback(implementer, revision_prompt(task, review, findings),
                                                         folder, unavailable, task_id, 'revision', quiet=quiet)
        except AgentUsageExhausted as exc:
            print(str(exc), file=sys.stderr)
            record('blocked', str(exc), task_id, phase='revision', resets=exc.resets)
            reasons = ['revision impossible: ' + str(exc)]
            break
        responses = normalize_responses(parse_marked_json(response, 'ATS_RESPONSES_JSON'))
        history[-1]['responses'] = responses
        answered = {r['id'] for r in responses}
        missing = [f['id'] for f in findings if f['id'] not in answered]
        record('feedback_response', response[-2000:], task_id, implementer=implementer, cycle=cycle,
               responses=responses, unanswered=missing)
        capture_learning_proposals(response, task_id)
    summary = 'Not approved; needs a human decision: ' + '; '.join(reasons)
    print('ATS: ' + summary, file=sys.stderr)
    record('escalated', summary, task_id, implementer=implementer, reviewer=reviewer,
           blocking_reasons=reasons, cycles=len(history), branch=branch)
    return False, reviewer, implementer, history


def preset_task_id():
    """ATS_TASK_ID lets a caller know the task id before the run starts."""
    value = os.environ.get('ATS_TASK_ID', '').strip()
    if not value:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise RuntimeError('ATS_TASK_ID must be a UUID: ' + value)


def task_flow(task, agent, push, quiet=False):
    conf=project()
    repository = execute(['git', 'rev-parse', '--show-toplevel'], check=False)
    local_mode = repository.returncode != 0
    if not local_mode and Path(repository.stdout.strip()).resolve() != APP.resolve():
        raise RuntimeError('Run ATS from the application repository root')
    if local_mode and push:
        raise RuntimeError('This project is not a Git repository. Run .ats task without --push; local changes will be exported as a patch.')
    agent=select(task) if agent=='auto' else agent
    task_id=preset_task_id() or str(uuid.uuid4())
    unavailable=UnavailableAgents()
    branch='ats/'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')+'-'+task_id[:8]
    folder=APP.parent / f'{APP.name}-ats-{task_id[:8]}'
    record('assigned',task,task_id,implementer=agent,branch=branch)
    if not (executable('codex') or executable('claude')):
        raise RuntimeError('Neither agent executable found; run .ats install')
    worktree_created=False
    try:
        if local_mode:
            make_local_workspace(folder, branch)
            print('ATS: project has no Git repository; using an isolated local snapshot.', flush=True)
        else:
            execute(['git','worktree','add','-b',branch,str(folder),'HEAD'])
        worktree_created=True
        base_commit = execute(['git', 'rev-parse', 'HEAD'], folder).stdout.strip()
        phase('implementation', task_id, implementer=agent, branch=branch, worktree=str(folder))
        prompt = (f"Read {ATS/'POLICY.md'} and {ATS/'AGENTS.md'}. Task: {task}. "
                  "Change only necessary application files and add tests. "
                  "Do not commit, push, merge, deploy, trade, expose secrets or modify .ats; "
                  "ATS commits your work after the review."
                  + knowledge_context()
                  + '\nAt the end include ONE LINE ATS_LEARNINGS_JSON: '
                  + '[{"kind":"finding","title":"<title>","observation":"<observation>",'
                  + '"evidence":"<evidence>"}] (replace the placeholders, or use []). '
                  + 'Only evidence-backed items, no secrets; proposals are not approved rules.')
        implementer,result=invoke_with_fallback(agent,prompt,folder,unavailable,task_id,'implementation',quiet=quiet)
        record('implementation',result[-1600:],task_id,implementer=implementer,branch=branch)
        capture_learning_proposals(result,task_id)
        approved,reviewer,implementer,history=review_loop(task,task_id,folder,branch,implementer,unavailable,conf,quiet=quiet,
                                                           base=base_commit)
        execute(['git','add','-A'],folder)
        staged = execute(['git', 'diff', '--cached', '--quiet'], folder, check=False).returncode != 0
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
        if push:
            phase('pr', task_id, branch=branch)
            execute(['git','push','-u','origin',branch],folder)
            if not shutil.which('gh'):
                print('gh missing: branch pushed but draft PR must be created manually');return
            base=conf.get('base_branch','main')
            outcome=execute(['gh','pr','create','--draft','--base',base,'--head',branch,'--title','ATS: '+task[:65],
                             '--body',f'Task ID: {task_id}\nImplementer: {implementer}\nReviewer: {reviewer}\nReview cycles: {len(history)}\nReview/check outcomes in ATS data. Human merge required.'],folder)
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
    p=sub.add_parser('task');p.add_argument('description',nargs='+');p.add_argument('--agent',choices=['auto','codex','claude'],default='auto');p.add_argument('--push',action='store_true');p.add_argument('--quiet',action='store_true',help='Hide live agent output and progress messages')
    p=sub.add_parser('event');p.add_argument('kind');p.add_argument('summary');p.add_argument('--task')
    p=sub.add_parser('run');p.add_argument('argv',nargs=argparse.REMAINDER)
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
        elif args.command=='task':task_flow(' '.join(args.description),args.agent,args.push,args.quiet)
        elif args.command=='event':record(args.kind,args.summary,args.task)
        elif args.command=='data-sync':sync_private_data()
        elif args.command=='learn':learn(args.kind,args.title,args.observation,args.evidence,args.task,args.confidence,'human')
        elif args.command=='run':
            argv=args.argv[1:] if args.argv and args.argv[0]=='--' else args.argv
            record('program_started',' '.join(argv)[:500]);exitcode=child_run(argv)
            record('program_stopped',f'exit code {exitcode}',exit_code=exitcode)
        elif args.command=='doctor':
            print(json.dumps({'app':str(APP),'ats':str(ATS),'cli':{x:bool(executable(x)) for x in ('git','codex','claude','gh')},'project_config':project()},indent=2))
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
