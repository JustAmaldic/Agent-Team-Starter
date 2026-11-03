"""Token usage and sessions of agent CLI calls, read from their structured output.

Claude Code: `claude -p --output-format json` prints one JSON object with the answer (`result`),
`session_id` and `usage` (input, cache creation, cache read and output tokens).
Codex: `codex exec --json` prints JSON lines; `thread.started` carries the session id,
`item.completed` the agent messages and `turn.completed` the usage (input includes cached input).
Older Codex versions print `{"msg": {...}}` lines; those are understood too.

The text ATS parses (findings, verdicts, responses) is rebuilt from the agent messages, so the
markers look exactly as in plain text mode. Without parsable JSON the raw output is used unchanged.
Nothing here stores prompts or answers; only counts, model names and session ids.
"""
from __future__ import annotations

import json
import threading

USAGE_FIELDS = ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens')
_local = threading.local()


# ---------------------------------------------------------------- per-thread call context
def context() -> dict:
    """What ATS is doing in this thread (task, purpose, subtask, cycle, budget)."""
    return getattr(_local, 'context', {})


def set_context(**fields) -> None:
    current = dict(context())
    current.update(fields)
    _local.context = current


def reset_context() -> None:
    _local.context = {}


def remember_call(info: dict | None) -> None:
    _local.last_call = info


def last_call() -> dict | None:
    """The parsed result (usage, session id, model) of this thread's latest agent call, if any."""
    return getattr(_local, 'last_call', None)


# ---------------------------------------------------------------- parsing
def _count(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def usage(input_tokens=None, output_tokens=None, cache_read_tokens=None, cache_write_tokens=None) -> dict:
    return {'input_tokens': _count(input_tokens), 'output_tokens': _count(output_tokens),
            'cache_read_tokens': _count(cache_read_tokens), 'cache_write_tokens': _count(cache_write_tokens)}


def billable(counts: dict) -> int:
    """Tokens that weigh on a usage limit: new input, cache writes and output in full, cache reads
    at a tenth (providers bill cache hits at roughly that rate)."""
    counts = counts or {}
    return ((counts.get('input_tokens') or 0) + (counts.get('output_tokens') or 0)
            + (counts.get('cache_write_tokens') or 0) + (counts.get('cache_read_tokens') or 0) // 10)


def context_size(counts: dict) -> int:
    """How much context the agent read in its latest call (new + cached input)."""
    counts = counts or {}
    return ((counts.get('input_tokens') or 0) + (counts.get('cache_read_tokens') or 0)
            + (counts.get('cache_write_tokens') or 0))


def _json_objects(stdout: str):
    for line in (stdout or '').splitlines():
        line = line.strip()
        if line.startswith('{') and line.endswith('}'):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                yield value


def parse_claude(stdout: str) -> dict | None:
    """The result object of `claude -p --output-format json`, or None when there is none."""
    candidates = list(_json_objects(stdout))
    if not candidates:
        try:
            whole = json.loads((stdout or '').strip())
            candidates = [whole] if isinstance(whole, dict) else []
        except ValueError:
            return None
    result = next((c for c in reversed(candidates) if c.get('type') == 'result'), None)
    if result is None:
        return None
    raw = result.get('usage') if isinstance(result.get('usage'), dict) else {}
    models = list((result.get('modelUsage') or {}).keys()) if isinstance(result.get('modelUsage'), dict) else []
    return {
        'text': str(result.get('result') or ''),
        'session_id': result.get('session_id') if isinstance(result.get('session_id'), str) else None,
        'is_error': bool(result.get('is_error')),
        'usage': usage(raw.get('input_tokens'), raw.get('output_tokens'),
                       raw.get('cache_read_input_tokens'), raw.get('cache_creation_input_tokens')),
        'model': ', '.join(models[:3]) or None,
        'turns': _count(result.get('num_turns')),
    }


def parse_codex(stdout: str) -> dict | None:
    """Agent messages, session id and summed usage of `codex exec --json`, or None without JSON."""
    messages, errors, session, model = [], [], None, None
    totals = dict.fromkeys(('input', 'cached', 'output'), 0)
    legacy_total = None
    seen = False
    for event in _json_objects(stdout):
        kind = event.get('type')
        msg = event.get('msg') if isinstance(event.get('msg'), dict) else None
        if kind is None and msg is None:
            continue
        seen = True
        if kind == 'thread.started' and isinstance(event.get('thread_id'), str):
            session = event['thread_id']
        elif kind == 'item.completed' and isinstance(event.get('item'), dict):
            item = event['item']
            if item.get('type') in ('agent_message', 'assistant_message') and item.get('text'):
                messages.append(str(item['text']))
            elif item.get('type') == 'error' and item.get('message'):
                errors.append(str(item['message']))
        elif kind == 'turn.completed' and isinstance(event.get('usage'), dict):
            u = event['usage']
            totals['input'] += _count(u.get('input_tokens')) or 0
            totals['cached'] += _count(u.get('cached_input_tokens')) or 0
            totals['output'] += _count(u.get('output_tokens')) or 0
        elif kind in ('turn.failed', 'error'):
            error = event.get('error') if isinstance(event.get('error'), dict) else event
            if error.get('message'):
                errors.append(str(error['message']))
        if msg:
            mtype = msg.get('type')
            if mtype == 'agent_message' and msg.get('message'):
                messages.append(str(msg['message']))
            elif mtype == 'session_configured':
                session = msg.get('session_id') if isinstance(msg.get('session_id'), str) else session
                model = msg.get('model') if isinstance(msg.get('model'), str) else model
            elif mtype == 'token_count':
                info = msg.get('info') if isinstance(msg.get('info'), dict) else msg
                total = info.get('total_token_usage') if isinstance(info.get('total_token_usage'), dict) else None
                legacy_total = total or legacy_total
            elif mtype in ('error', 'stream_error') and msg.get('message'):
                errors.append(str(msg['message']))
    if not seen:
        return None
    if legacy_total and not any(totals.values()):
        totals = {'input': _count(legacy_total.get('input_tokens')) or 0,
                  'cached': _count(legacy_total.get('cached_input_tokens')) or 0,
                  'output': _count(legacy_total.get('output_tokens')) or 0}
    text = '\n\n'.join(messages)
    if errors:
        text = (text + '\n' if text else '') + '\n'.join(errors)
    counted = any(totals.values())
    return {
        'text': text, 'session_id': session, 'is_error': bool(errors) and not messages, 'model': model,
        # Codex counts cached input inside input_tokens; ATS keeps new and cached input apart.
        'usage': usage(max(0, totals['input'] - totals['cached']) if counted else None,
                       totals['output'] if counted else None, totals['cached'] if counted else None, None),
        'turns': None,
    }


def render_codex_line(line: str) -> str | None:
    """A readable live line for one `codex exec --json` line (None hides it)."""
    stripped = line.strip()
    if not (stripped.startswith('{') and stripped.endswith('}')):
        return line
    try:
        event = json.loads(stripped)
    except ValueError:
        return line
    if not isinstance(event, dict):
        return line
    kind = event.get('type')
    item = event.get('item') if isinstance(event.get('item'), dict) else {}
    if kind == 'item.completed' and item.get('type') in ('agent_message', 'assistant_message'):
        return str(item.get('text', '')) + '\n'
    if kind == 'item.started' and item.get('type') == 'command_execution':
        return '$ ' + str(item.get('command', ''))[:300] + '\n'
    if kind == 'item.completed' and item.get('type') == 'file_change':
        changes = item.get('changes') if isinstance(item.get('changes'), list) else []
        return 'edited ' + ', '.join(str(c.get('path', '')) for c in changes if isinstance(c, dict))[:300] + '\n'
    if kind in ('turn.failed', 'error'):
        error = event.get('error') if isinstance(event.get('error'), dict) else event
        return 'ERROR: ' + str(error.get('message', ''))[:600] + '\n'
    msg = event.get('msg') if isinstance(event.get('msg'), dict) else {}
    if msg.get('type') == 'agent_message':
        return str(msg.get('message', '')) + '\n'
    if msg.get('type') in ('error', 'stream_error'):
        return 'ERROR: ' + str(msg.get('message', ''))[:600] + '\n'
    return None


def render_claude_line(line: str) -> str | None:
    """Claude prints a single result object at the end; show its answer instead of the JSON."""
    parsed = parse_claude(line)
    if parsed is None:
        return line
    return parsed['text'] + ('\n' if not parsed['text'].endswith('\n') else '')


PARSERS = {'claude': parse_claude, 'codex': parse_codex}
RENDERERS = {'claude': render_claude_line, 'codex': render_codex_line}


# ---------------------------------------------------------------- sessions (implementer only)
class Sessions:
    """The implementer's CLI session per (task, subtask, agent), so a revision can continue it.

    A session is only offered back when the worktree is exactly as the agent left it (tree hash),
    and while it has not grown past the turn and context limits; otherwise ATS starts a new session
    with a compact handoff. Reviewers never get a session from here: they always start fresh.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}

    def remember(self, key, session_id, tree, counts):
        if not session_id:
            return
        with self._lock:
            old = self._sessions.get(key)
            turns = (old['turns'] + 1) if old and old['session_id'] == session_id else 1
            self._sessions[key] = {'session_id': session_id, 'tree': tree, 'turns': turns,
                                   'context_tokens': context_size(counts)}

    def update_tree(self, key, tree):
        with self._lock:
            if key in self._sessions:
                self._sessions[key]['tree'] = tree

    def usable(self, key, tree, max_turns=4, max_context_tokens=250000):
        """(session id, None) when it may be continued, else (None, reason)."""
        with self._lock:
            saved = self._sessions.get(key)
        if not saved:
            return None, 'no session'
        if saved['tree'] != tree:
            return None, 'worktree changed outside the session'
        if saved['turns'] >= max_turns:
            return None, f"session reached {saved['turns']} turns"
        if saved['context_tokens'] > max_context_tokens:
            return None, f"session context {saved['context_tokens']} tokens is above {max_context_tokens}"
        return saved['session_id'], None

    def forget(self, key):
        with self._lock:
            self._sessions.pop(key, None)
