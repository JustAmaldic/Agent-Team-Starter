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
    # Explicit shared contracts are reviewed as their own DAG nodes before consumers run.
    providers = {}
    for node in nodes:
        for field in ("contracts_provided", "contracts_required"):
            values = node.get(field, [])
            if (not isinstance(values, list) or len(values) > 20 or
                    not all(isinstance(v, str) and ID.fullmatch(v) for v in values) or
                    len(set(values)) != len(values)):
                raise ValueError("Invalid contract references")
        for name in node.get("contracts_provided", []):
            if name in providers:
                raise ValueError("Shared contract must have one owner: " + name)
            providers[name] = node["id"]
    depends = {node["id"]: set(node.get("depends_on", [])) for node in nodes}
    def upstream(name):
        result, pending = set(), list(depends[name])
        while pending:
            item = pending.pop()
            if item not in result:
                result.add(item)
                pending.extend(depends.get(item, ()))
        return result
    for node in nodes:
        for name in node.get("contracts_required", []):
            provider = providers.get(name)
            if provider is None:
                raise ValueError("Missing contract provider: " + name)
            if provider not in upstream(node["id"]):
                raise ValueError("Contract must be reviewed before consumer: " + name)
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
         "owned_paths": list(n["owned_paths"]),
         **({key: list(n[key]) for key in ("contracts_provided", "contracts_required") if key in n}),
         # An existing file or screen whose layout and components a UI part should follow.
         **({"style_reference": n["style_reference"].strip()[:200]}
            if isinstance(n.get("style_reference"), str) and n["style_reference"].strip() else {})}
        for n in nodes
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


def critical_path(plan):
    """{task id: number of tasks on the longest chain from it to the end, itself included}."""
    graph = validate_dag(plan)
    dependents = {n["id"]: [] for n in graph["tasks"]}
    for node in graph["tasks"]:
        for dep in node["depends_on"]:
            dependents[dep].append(node["id"])
    lengths = {}

    def length(name):
        if name not in lengths:
            lengths[name] = 1 + max((length(child) for child in dependents[name]), default=0)
        return lengths[name]

    for node in graph["tasks"]:
        length(node["id"])
    return lengths


def execution_batches(plan, max_parallel=2):
    """Deterministic ready-queue batches. Every batch passes review/merge before dependents run.

    Among the ready tasks the one with the longest remaining chain goes first, so the wait for a
    batch to finish never holds back the critical path while a side task could have waited.
    """
    if max_parallel != 2:
        raise ValueError("ATS currently supports exactly two concurrent worktrees")
    graph = validate_dag(plan)
    chain = critical_path(graph)
    order = {n["id"]: i for i, n in enumerate(graph["tasks"])}
    completed, batches = set(), []
    while len(completed) < len(graph["tasks"]):
        ready = sorted(ready_tasks(graph, completed), key=lambda n: (-chain[n["id"]], order[n["id"]]))
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
