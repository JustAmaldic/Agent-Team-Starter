"""Agent limits, quota detection and sandbox safety classification.

Independent from repository, UI, network and task orchestration.
"""
import re


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


