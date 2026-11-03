"""Budgets and loop detection per (sub)task.

A budget caps the tokens (as counted by usage.billable) and the wall time one part may spend. One
repair counter covers every automatic repair (pre-review gate and failing tests), so there are no
two limits that contradict each other. The stuck detector notices the same failure twice in a row,
a finding that comes back after it was answered as fixed, and a revision that changed nothing.
When either trips, ATS stops the part and escalates with what was repeated.
"""
from __future__ import annotations

import os
import re
import threading
import time

from . import usage

DEFAULT_TOKENS = 2_000_000
DEFAULT_SECONDS = 3600
DEFAULT_REPAIRS = 3


def multiplier() -> float:
    """ATS_BUDGET_MULTIPLIER raises (or lowers) every budget, e.g. for a retry with more room."""
    try:
        value = float(os.environ.get('ATS_BUDGET_MULTIPLIER', '1'))
    except ValueError:
        return 1.0
    return value if 0.1 <= value <= 20 else 1.0


class Budget:
    def __init__(self, tokens=DEFAULT_TOKENS, seconds=DEFAULT_SECONDS, repairs=DEFAULT_REPAIRS, clock=time.monotonic):
        factor = multiplier()
        self.tokens = int(tokens * factor)
        self.seconds = int(seconds * factor)
        self.repairs = int(repairs)
        self.used_tokens = 0
        self.repairs_used = 0
        self._clock = clock
        self._started = clock()
        self._lock = threading.Lock()

    @classmethod
    def for_part(cls, conf, owned_paths=()):
        """Bigger parts (more owned paths) get proportionally more room."""
        tokens = int(conf.get('subtask_token_budget', DEFAULT_TOKENS))
        extra = max(0, len(owned_paths) - 3)
        return cls(tokens=tokens + extra * tokens // 6,
                   seconds=int(conf.get('subtask_time_budget', DEFAULT_SECONDS)) + extra * 300,
                   repairs=int(conf.get('max_repairs', DEFAULT_REPAIRS)))

    def add(self, counts):
        with self._lock:
            self.used_tokens += usage.billable(counts)

    def elapsed(self):
        return int(self._clock() - self._started)

    def exceeded(self):
        """A reason when the budget is spent, else None."""
        if self.used_tokens > self.tokens:
            return f'token budget spent ({self.used_tokens} of {self.tokens})'
        if self.elapsed() > self.seconds:
            return f'time budget spent ({self.elapsed()}s of {self.seconds}s)'
        return None

    def take_repair(self):
        """True while automatic repairs are left (and counts this one)."""
        with self._lock:
            if self.repairs_used >= self.repairs:
                return False
            self.repairs_used += 1
            return True

    def snapshot(self):
        return {'token_budget': self.tokens, 'tokens_used': self.used_tokens, 'time_budget': self.seconds,
                'seconds_used': self.elapsed(), 'repairs': self.repairs, 'repairs_used': self.repairs_used}


def finding_key(finding):
    """File plus normalised summary: the same problem, even when the reviewer renumbers it."""
    summary = re.sub(r'[^a-z0-9 ]+', ' ', str(finding.get('summary', '')).lower())
    words = [w for w in summary.split() if len(w) > 2][:8]
    return (str(finding.get('file', '')).strip().lower(), ' '.join(words))


class StuckDetector:
    def __init__(self):
        self._last_failure = {}
        self._fixed = {}

    def same_failure(self, signature, channel='checks'):
        """A reason when exactly this failure also happened on the previous attempt (per kind of check)."""
        if not signature:
            self._last_failure.pop(channel, None)
            return None
        repeated = signature == self._last_failure.get(channel)
        self._last_failure[channel] = signature
        if repeated:
            names = ', '.join(sorted({name for name, _ in signature}))
            return f'the same check keeps failing after a repair: {names}'
        return None

    def answered(self, findings, responses):
        """Remember which findings the implementer said it fixed."""
        status = {r['id']: r['status'] for r in responses}
        for finding in findings:
            if status.get(finding['id']) == 'fixed':
                key = finding_key(finding)
                self._fixed[key] = self._fixed.get(key, 0) + 1

    def reopened(self, findings):
        """A reason when a finding answered as fixed is reported open again, a second time."""
        for finding in findings:
            if self._fixed.get(finding_key(finding), 0) >= 2:
                return (f"finding {finding['id']} ({finding.get('file') or 'no file'}) came back after "
                        'being answered as fixed twice')
        return None

    @staticmethod
    def no_progress(tree_before, tree_after):
        if tree_before and tree_before == tree_after:
            return 'the revision changed no files'
        return None
