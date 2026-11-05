"""Durable, content-bound checkpoints for independently reviewed ATS work."""
import hashlib
import json
import os
import tempfile
from pathlib import Path


def plan_digest(plan):
    return hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def node_digest(node):
    """What a DAG node asks for: title, dependencies, owned files and contracts (not its position)."""
    keep = {k: node.get(k) for k in ("id", "title", "depends_on", "owned_paths",
                                      "contracts_provided", "contracts_required") if k in node}
    return hashlib.sha256(json.dumps(keep, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def node_digests(plan):
    if not isinstance(plan, dict) or plan.get("plan_version") != 2:
        return {}
    return {node["id"]: node_digest(node) for node in plan.get("tasks", [])}


def routing_digest(environ=None):
    """The model routing of the run (ATS_MODEL_* variables): recorded so a resume can say it changed."""
    environ = os.environ if environ is None else environ
    routing = {k: v for k, v in sorted(environ.items()) if k.startswith("ATS_MODEL_") or k == "ATS_ANTIGRAVITY_MODEL"}
    return hashlib.sha256(json.dumps(routing, sort_keys=True).encode("utf-8")).hexdigest()


def compatible_prefix(plan, saved_digests, saved_nodes):
    """How many completed nodes of an earlier plan can be reused under a changed plan.

    A node is reusable when it is unchanged (same digest) and everything it depends on is reusable
    too. Reuse follows the execution batches of the new plan, so a changed node, and every node
    after it in that order, is planned again. Returns (count, changed node ids)."""
    from ats_modules import dag
    if not isinstance(plan, dict) or not isinstance(saved_digests, dict) or not isinstance(saved_nodes, list):
        return 0, []
    current = node_digests(plan)
    done = set(saved_nodes)
    reusable = {n for n in done if n in current and current[n] == saved_digests.get(n)}
    changed = sorted(n for n in done if n not in reusable)
    count = 0
    for batch in dag.execution_batches(plan):
        ids = [node["id"] for node in batch]
        if not all(i in reusable for i in ids):
            break
        count += len(ids)
    return count, changed


def checkpoint_path(data, task_id):
    if not isinstance(task_id, str) or not task_id or not all(c.isalnum() or c == "-" for c in task_id):
        raise ValueError("Invalid checkpoint task id")
    return Path(data) / "checkpoints" / (task_id + ".json")


def save_checkpoint(data, task_id, plan, worktree, commit, completed, attribution, pending=None):
    """Save only after reviewed code has been committed, never raw agent output."""
    if not isinstance(commit, str) or len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise ValueError("Checkpoint requires a complete Git SHA")
    if not isinstance(completed, int) or completed < 0 or not isinstance(attribution, list):
        raise ValueError("Invalid checkpoint progress")
    value = {
        "version": 1, "task_id": task_id, "plan_digest": plan_digest(plan),
        "worktree": str(Path(worktree).resolve()), "commit": commit,
        "completed": completed, "attribution": attribution,
        "pending": list(pending or []),
        "node_digests": node_digests(plan),
        "routing_digest": routing_digest(),
    }
    if isinstance(plan, dict):
        try:
            value["completed_nodes"] = completed_node_ids(plan, completed)
        except ValueError:
            value["completed_nodes"] = []
    path = checkpoint_path(data, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".checkpoint-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return value


def load_checkpoint(data, task_id, plan, worktree, head):
    """Reject stale checkpoints. A retry must not trust a different code snapshot."""
    path = checkpoint_path(data, task_id)
    if not path.is_file() or path.stat().st_size > 65536:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("version") != 1:
        return None
    if (value.get("task_id") != task_id
            or value.get("worktree") != str(Path(worktree).resolve())
            or value.get("commit") != head or type(value.get("completed")) is not int):
        return None
    value["routing_changed"] = value.get("routing_digest") not in (None, routing_digest())
    if value.get("plan_digest") == plan_digest(plan):
        return value
    # The plan changed: reuse only unchanged nodes (and what they depend on); plan the rest again.
    # A completed node that changed has its old code in the worktree: never build on that.
    count, changed = compatible_prefix(plan, value.get("node_digests"), value.get("completed_nodes"))
    if changed or count != value["completed"] or count == 0:
        return None
    saved = value.get("node_digests") or {}
    value.update(pending=[], plan_changed=True,
                 replanned=sorted(n for n, d in node_digests(plan).items() if saved.get(n) != d))
    return value


def read_checkpoint(data, task_id):
    """Read an untrusted prior record; caller must verify plan, worktree and HEAD."""
    path = checkpoint_path(data, task_id)
    if not path.is_file() or path.stat().st_size > 65536:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("version") != 1:
        return None
    return value


def completed_node_ids(plan, completed):
    """Map reviewed batch prefixes to exact DAG task IDs, without marking partial pairs done."""
    from ats_modules import dag
    if not isinstance(plan, dict) or plan.get("plan_version") != 2:
        return []
    batches = dag.execution_batches(plan)
    result = []
    for batch in batches:
        if len(result) + len(batch) > completed:
            break
        result.extend(node["id"] for node in batch)
    if len(result) != completed:
        raise ValueError("Checkpoint splits a parallel DAG batch")
    return result


def verified_resume(data, source_id, plan, worktree, head):
    """Resume only checkpointed reviewed work from the same immutable code snapshot."""
    record = load_checkpoint(data, source_id, plan, worktree, head)
    if not record:
        return None
    from ats_modules import dag
    total = len(dag.validate_dag(plan)["tasks"]) if isinstance(plan, dict) else sum(
        len(w) if isinstance(w, list) else 1 for w in plan)
    count = record["completed"]
    if not 0 <= count <= total:
        return None
    if count == 0 and not record.get("pending"):
        return None
    attribution = record.get("attribution")
    if not isinstance(attribution, list) or len(attribution) != count:
        return None
    numbers = [a.get("subtask") for a in attribution if isinstance(a, dict)
               and a.get("implementer") and a.get("reviewer")]
    if numbers != list(range(1, count + 1)):
        return None
    if isinstance(plan, dict):
        try:
            record["completed_nodes"] = completed_node_ids(plan, count)
        except ValueError:
            return None
    pending = record.get("pending", [])
    if not isinstance(pending, list) or len(pending) > 2:
        return None
    for node in pending:
        if (not isinstance(node, dict) or
            type(node.get("subtask")) is not int or
            node["subtask"] <= count or node["subtask"] > total or
            not all(isinstance(node.get(k), str) and node[k] for k in
                    ("commit", "worktree", "implementer", "reviewer", "wave_base"))):
            return None
    return record
