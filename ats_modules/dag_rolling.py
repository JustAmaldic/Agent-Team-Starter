"""Rolling DAG execution (t49): no wave barrier for the task worktree.

With waves, the faster part of a pair waits for the slower one before anything new starts. In
rolling mode (`dag_rolling`, opt-in, plan_version 2 only) the task worktree ("main" slot) starts the
next ready part as soon as its own part is reviewed and committed, while the side worktree keeps
working. A finished side part waits only until the main slot is between parts, then it is merged
(merge_parallel_part, unchanged). Every part is still reviewed independently and dependents only
start after their prerequisites are merged.

This module only decides what runs where; it never touches Git or agents.
"""
from __future__ import annotations

from . import dag as dag_planning

SLOTS = ("main", "side")


class Pipeline:
    def __init__(self, plan):
        self.graph = dag_planning.validate_dag(plan)
        self.nodes = {n["id"]: n for n in self.graph["tasks"]}
        self.chain = dag_planning.critical_path(self.graph)
        self.order = {n["id"]: i for i, n in enumerate(self.graph["tasks"])}
        self.merged: set[str] = set()
        self.running: dict[str, str] = {}   # slot -> node id
        self.waiting: str | None = None     # side part reviewed, not merged yet

    def busy(self, slot: str) -> bool:
        return slot in self.running or (slot == "side" and self.waiting is not None)

    def next_for(self, slot: str):
        """The ready part this slot should start, or None. Ready: every prerequisite merged, not
        started, and its files do not overlap a part that is running or waiting to be merged."""
        if slot not in SLOTS or self.busy(slot):
            return None
        taken = set(self.merged) | set(self.running.values()) | ({self.waiting} if self.waiting else set())
        locked = [self.nodes[i]["owned_paths"] for i in taken - self.merged]
        ready = [n for n in self.graph["tasks"] if n["id"] not in taken and set(n["depends_on"]) <= self.merged
                 and not any(dag_planning.paths_conflict(n["owned_paths"], paths) for paths in locked)]
        ready.sort(key=lambda n: (-self.chain[n["id"]], self.order[n["id"]]))
        return ready[0] if ready else None

    def start(self, slot: str, node_id: str) -> None:
        if self.busy(slot) or node_id not in self.nodes:
            raise ValueError("slot busy or unknown part")
        self.running[slot] = node_id

    def finished(self, slot: str) -> str:
        """A slot's part was approved: main parts are merged at once (they were built in the task
        worktree); a side part waits for merge_ready()."""
        node_id = self.running.pop(slot)
        if slot == "main":
            self.merged.add(node_id)
        else:
            self.waiting = node_id
        return node_id

    def merge_ready(self) -> bool:
        """The side part can be merged now: the task worktree has no part in progress."""
        return self.waiting is not None and "main" not in self.running

    def merged_side(self) -> str:
        node_id, self.waiting = self.waiting, None
        self.merged.add(node_id)
        return node_id

    def done(self) -> bool:
        return len(self.merged) == len(self.nodes)

    def stuck(self) -> bool:
        return not self.done() and not self.running and self.waiting is None and \
            self.next_for("main") is None and self.next_for("side") is None
