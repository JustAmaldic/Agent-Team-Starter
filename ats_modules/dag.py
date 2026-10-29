"""Versioned dependency graph for bounded, reviewable ATS tasks."""
from __future__ import annotations

import fnmatch
import re

MAX_NODES = 20
ID = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")


def validate_dag(plan):
    if not isinstance(plan, dict) or plan.get("plan_version") != 2:
        raise ValueError("DAG requires plan_version 2")
    nodes = plan.get("tasks")
    if not isinstance(nodes, list) or not 2 <= len(nodes) <= MAX_NODES:
        raise ValueError("DAG needs 2 to 20 tasks")
    ids = set()
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not ID.fullmatch(node["id"]):
            raise ValueError("Invalid DAG task id")
        if node["id"] in ids:
            raise ValueError("Duplicate DAG task id")
        ids.add(node["id"])
        if not isinstance(node.get("title"), str) or not 12 <= len(node["title"].strip()) <= 1000:
            raise ValueError("DAG task needs bounded description")
        deps = node.get("depends_on", [])
        paths = node.get("owned_paths", [])
        if not isinstance(deps, list) or len(set(map(str, deps))) != len(deps) or not all(isinstance(x, str) for x in deps):
            raise ValueError("Invalid dependencies")
        if not isinstance(paths, list) or not paths or len(paths) > 30 or not all(
            isinstance(p, str) and p.strip() and not p.startswith(("/", "\\\\")) and
            ".." not in p.split("/") and "\\\\" not in p for p in paths
        ):
            raise ValueError("DAG tasks need safe, explicit owned_paths")
    for node in nodes:
        if any(dep not in ids or dep == node["id"] for dep in node.get("depends_on", [])):
            raise ValueError("Unknown or self dependency")
    remaining = {n["id"]: set(n.get("depends_on", [])) for n in nodes}
    resolved = set()
    while remaining:
        ready = [name for name, deps in remaining.items() if deps <= resolved]
        if not ready:
            raise ValueError("Cyclic DAG")
        resolved.update(ready)
        for name in ready:
            del remaining[name]
    return {"plan_version": 2, "tasks": [
        {"id": n["id"], "title": n["title"].strip(), "depends_on": list(n.get("depends_on", [])),
         "owned_paths": list(n["owned_paths"])} for n in nodes
    ]}


def paths_conflict(a, b):
    """Conservative lock checking: folder prefixes and glob overlap block concurrent writes."""
    for left in a:
        for right in b:
            x, y = left.rstrip("/"), right.rstrip("/")
            if (x == y or x.startswith(y + "/") or y.startswith(x + "/") or
                    any(c in x for c in "*?[") or any(c in y for c in "*?[") or
                    fnmatch.fnmatch(x, y) or fnmatch.fnmatch(y, x)):
                return True
    return False


def ready_tasks(plan, completed, running=()):
    graph = validate_dag(plan)
    done, busy = set(completed), set(running)
    known = {n["id"] for n in graph["tasks"]}
    if not done <= known or not busy <= known:
        raise ValueError("Unknown task state")
    return [n for n in graph["tasks"] if n["id"] not in done | busy
            and set(n["depends_on"]) <= done]


def execution_batches(plan, max_parallel=2):
    """Deterministic ready-queue batches. Every batch passes review/merge before dependents run."""
    if max_parallel != 2:
        raise ValueError("ATS currently supports exactly two concurrent worktrees")
    graph = validate_dag(plan)
    completed, batches = set(), []
    while len(completed) < len(graph["tasks"]):
        ready = ready_tasks(graph, completed)
        if not ready:
            raise ValueError("DAG cannot progress")
        batch = [ready[0]]
        for candidate in ready[1:]:
            if len(batch) == max_parallel:
                break
            if all(not paths_conflict(candidate["owned_paths"], n["owned_paths"]) for n in batch):
                batch.append(candidate)
        batches.append(batch)
        completed.update(n["id"] for n in batch)
    return batches


def as_waves(plan):
    """Project a DAG onto the existing reviewed two-worktree execution engine."""
    return [parts[0]["title"] if len(parts) == 1 else [p["title"] for p in parts]
            for parts in execution_batches(plan)]
