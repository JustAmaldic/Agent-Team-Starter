"""Minimum severity for findings whose impact is never minor.

A reviewer once rated "crafted input causes heavy backtracking that holds the GIL" (a request that
freezes the whole host) as MINOR, so ATS approved it. The severity a reviewer picks is a judgement;
some kinds of problem are always at least MAJOR, whatever the reasoning about who can trigger them:

- the host, server or event loop can be blocked (catastrophic regex backtracking, ReDoS, DoS);
- data can be lost or corrupted;
- injection, path traversal, or a way around authentication, authorization or CSRF;
- a secret or credential can leak.

ATS raises such findings to MAJOR, so they block approval until fixed or decided by a human. The
patterns are deliberately specific: a finding that merely mentions "token" or "performance" stays as
the reviewer rated it.
"""
from __future__ import annotations

import re

HIGH_IMPACT = re.compile(
    r"re\s?dos|catastrophic(ally)?\s+backtrack|heavy\s+backtrack|exponential\s+backtrack|"
    r"(cubic|quadratic|exponential)\s+(work|time|backtracking)|"
    r"denial[\s-]of[\s-]service|\bdos\b|"
    r"(freez\w*|stall\w*|hang\w*|block\w*)\s+(the\s+)?(whole\s+|entire\s+)?(host|server|event\s+loop|process)|"
    r"holds?\s+the\s+gil|"
    r"data\s+loss|los(e|es|ing)\s+(user\s+|all\s+|the\s+)?data|corrupt\w*\s+(the\s+)?(data|database|storage|file)|"
    r"(sql|command|shell|code|template)\s+injection|path\s+traversal|\bxss\b|"
    r"(bypass\w*|circumvent\w*|skip\w*)\s+(the\s+)?(auth\w*|csrf|login|permission\w*)|privilege\s+escalation|"
    r"(leak\w*|expos\w*)\s+(a\s+|the\s+)?(secret|token|credential|password|api\s+key)s?",
    re.IGNORECASE)
FLOOR = 'MAJOR'
LOWER = ('MINOR', 'SUGGESTION')


def raise_floor(findings):
    """Raise high-impact MINOR/SUGGESTION findings to MAJOR. Returns (findings, raised ids)."""
    raised = []
    result = []
    for finding in findings:
        text = f"{finding.get('summary', '')} {finding.get('detail', '')}"
        if finding.get('severity') in LOWER and HIGH_IMPACT.search(text):
            finding = dict(finding, severity=FLOOR, raised_from=finding['severity'])
            raised.append(finding['id'])
        result.append(finding)
    return result, raised
