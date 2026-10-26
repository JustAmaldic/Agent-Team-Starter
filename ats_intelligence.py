"""Deterministic task safety and evidence-backed agent metrics."""
import json
import re
from collections import defaultdict
from pathlib import Path

CATEGORIES = {
    "python": (r"\bpython\b", r"\.py\b", r"pytest", r"django", r"fastapi"),
    "frontend": (r"\bcss\b", r"react", r"frontend", r"html", r"javascript", r"typescript"),
    "tests": (r"\btests?\b", r"pytest", r"unittest", r"coverage"),
    "architecture": (r"architecture", r"architectuur", r"design system", r"schema"),
    "refactoring": (r"refactor", r"cleanup", r"restructure"),
    "documentation": (r"readme", r"documentatie", r"docs", r"documentation"),
}
HIGH_RISK = {
    "authentication": (r"oauth", r"authenticat", r"authorization", r"login", r"jwt", r"password", r"secret"),
    "database": (r"migrat", r"\bdatabase\b", r"\bsql\b", r"drop table", r"schema change"),
    "finance": (r"\btrad(?:e|ing|er)\b", r"payment", r"betal", r"transfer funds", r"wallet"),
    "production": (r"deploy", r"production", r"productie", r"terraform", r"infrastructure"),
    "security": (r"encrypt", r"cryptograph", r"security", r"beveilig", r"permission", r"firewall"),
}
MEDIUM_RISK = (r"\bapi\b", r"dependency", r"upgrade", r"package", r"config", r"backend")


def categories(task):
    return [category for category, patterns in CATEGORIES.items()
            if any(re.search(p, task, re.IGNORECASE) for p in patterns)] or ["general"]


def risk_profile(task, paths=()):
    text = " ".join([task, *map(str, paths)])
    reasons = [name for name, patterns in HIGH_RISK.items()
               if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)]
    medium = any(re.search(pattern, text, re.IGNORECASE) for pattern in MEDIUM_RISK)
    level = "high" if reasons else "medium" if medium else "low"
    return {
        "level": level, "reasons": reasons,
        "require_human_approval": level == "high",
        "security_review": bool(set(reasons) & {"authentication", "security", "finance"}),
        "extra_tests": level in ("medium", "high"),
        "auto_approve_knowledge": level != "high",
    }


def performance(events, min_samples=3):
    """Summarize completed work from task-tagged events, avoiding unverified success claims."""
    by_task = defaultdict(list)
    for event in events:
        if event.get("task_id") and event.get("event_type") in (
            "assigned", "finished", "review_result", "usage", "token_usage"
        ):
            by_task[event["task_id"]].append(event)
    stats = defaultdict(lambda: {"tasks": 0, "approved": 0, "revisions": 0,
                                 "tokens": 0, "duration_seconds": 0, "samples": 0})
    for event_group in by_task.values():
        assigned = next((e for e in event_group if e["event_type"] == "assigned"), None)
        finished = next((e for e in event_group if e["event_type"] == "finished"), None)
        if not assigned or not finished:
            continue
        agent = assigned.get("implementer")
        if not agent:
            continue
        task_types = categories(assigned.get("summary", ""))
        reviews = [e for e in event_group if e["event_type"] == "review_result"]
        revision_count = max(0, len(reviews) - 1)
        for category in task_types:
            row = stats[(agent, category)]
            row["tasks"] += 1
            row["approved"] += int(finished.get("approved") is True)
            row["revisions"] += revision_count
            row["tokens"] += sum(int(e.get("tokens", 0) or 0) for e in event_group
                                 if e["event_type"] in ("usage", "token_usage"))
            row["samples"] += 1
    return [{"agent": agent, "category": category, **values,
             "approval_rate": round(values["approved"] / values["tasks"], 3),
             "reliable": values["samples"] >= min_samples}
            for (agent, category), values in sorted(stats.items())]


def relation_issues(items, relations):
    """Report conflicting, superseded and missing knowledge links; do not auto-delete."""
    index = {item["id"]: item for item in items if "id" in item}
    findings = []
    valid = {"supports", "contradicts", "supersedes", "confirmed_by"}
    for relation in relations:
        source, target, kind = (relation.get(k) for k in ("source", "target", "kind"))
        if source not in index or target not in index or kind not in valid or source == target:
            findings.append({"type": "invalid", "relation": relation})
        elif kind == "contradicts":
            findings.append({"type": "conflict", "source": source, "target": target,
                             "needs_human": True})
        elif kind == "supersedes":
            findings.append({"type": "stale_candidate", "source": target,
                             "replacement": source, "needs_human": True})
    return findings


def pr_feedback_items(comments, existing_ids=()):
    """Convert PR comments to revision candidates with stable source identifiers."""
    seen = set(existing_ids)
    result = []
    for comment in comments:
        cid = comment.get("id")
        text = str(comment.get("body", "")).strip()
        if cid is None or not text:
            continue
        key = "github-pr-comment:" + str(cid)
        if key in seen:
            continue
        seen.add(key)
        result.append({"id": key, "body": text[:4000],
                       "path": comment.get("path"), "line": comment.get("line"),
                       "state": "needs_review"})
    return result


def read_events(directory):
    events = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                events.append(data)
        except (OSError, ValueError):
            continue
    return events
