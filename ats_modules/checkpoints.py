"""Durable, content-bound checkpoints for independently reviewed ATS work."""
import hashlib
import json
import os
import tempfile
from pathlib import Path


def plan_digest(plan):
    return hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


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
    }
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
    if (value.get("task_id") != task_id or value.get("plan_digest") != plan_digest(plan)
            or value.get("worktree") != str(Path(worktree).resolve())
            or value.get("commit") != head or type(value.get("completed")) is not int):
        return None
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
