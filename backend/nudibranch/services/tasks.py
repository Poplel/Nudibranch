import json
import socket
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from nudibranch.db.models import ProposalBatch, ProposalStatus, Task, TaskStatus


def enqueue_task(session: Session, task_type: str, payload: dict) -> Task:
    payload_json = json.dumps(payload, sort_keys=True)
    existing_query = (
        select(Task)
        .where(Task.type == task_type)
        .where(Task.status.in_([TaskStatus.queued, TaskStatus.running]))
        .order_by(Task.created_at.asc())
        .limit(1)
    )
    existing_query = existing_query.where(Task.payload_json == payload_json)

    existing = session.scalar(
        existing_query
    )
    if existing:
        return existing

    task = Task(type=task_type, payload_json=payload_json)
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


def task_to_payload(task: Task) -> dict:
    return json.loads(task.payload_json or "{}")


def task_result(task: Task) -> dict | None:
    if not task.result_json:
        return None
    return json.loads(task.result_json)


def claim_next_task(session: Session, lease_seconds: int = 300) -> Task | None:
    worker_id = socket.gethostname()
    now = datetime.now(timezone.utc)
    candidate = session.scalar(
        select(Task)
        .where(
            or_(
                Task.status == TaskStatus.queued,
                and_(Task.status == TaskStatus.running, Task.lease_until < now),
            )
        )
        .order_by(Task.created_at.asc())
        .limit(1)
    )
    if not candidate:
        return None

    result = session.execute(
        update(Task)
        .where(Task.id == candidate.id)
        .where(
            or_(
                Task.status == TaskStatus.queued,
                and_(Task.status == TaskStatus.running, Task.lease_until < now),
            )
        )
        .values(
            status=TaskStatus.running,
            attempts=Task.attempts + 1,
            locked_by=worker_id,
            lease_until=Task.lease_expiry(lease_seconds),
        )
    )
    session.commit()
    if result.rowcount != 1:
        return None
    return session.get(Task, candidate.id)


def complete_task(session: Session, task: Task, result: dict) -> None:
    task.status = TaskStatus.completed
    result = merge_task_logs(task, result)
    task.result_json = json.dumps(result)
    task.error = None
    task.lease_until = None
    session.commit()


def update_task_progress(session: Session, task: Task, current: int, total: int, message: str, **extra: object) -> None:
    payload = task_result(task) or {}
    progress = {
        "current": current,
        "total": total,
        "percent": round((current / total) * 100, 1) if total else 0,
        "message": message,
        **extra,
    }
    payload["progress"] = progress
    payload["logs"] = append_log_entry(payload.get("logs"), message, "info", {"progress": progress})
    task.result_json = json.dumps(payload)
    session.commit()


def fail_task(session: Session, task: Task, error: str) -> None:
    task.status = TaskStatus.failed
    task.error = error
    task.lease_until = None
    session.commit()


def append_task_log(session: Session, task: Task | None, message: str, level: str = "info", **context: Any) -> None:
    if task is None:
        return
    payload = task_result(task) or {}
    payload["logs"] = append_log_entry(payload.get("logs"), message, level, context)
    task.result_json = json.dumps(payload)
    session.commit()


def append_log_entry(existing: Any, message: str, level: str = "info", context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    logs = existing if isinstance(existing, list) else []
    if logs and isinstance(logs[-1], dict) and logs[-1].get("message") == message and logs[-1].get("level") == level:
        return logs
    entry = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "message": message,
    }
    if context:
        entry["context"] = context
    return [*logs, entry][-300:]


def merge_task_logs(task: Task, result: dict) -> dict:
    existing = task_result(task) or {}
    if existing.get("logs") and "logs" not in result:
        result = {**result, "logs": existing["logs"]}
    return result


def cancel_task(session: Session, task_id: str) -> Task:
    task = session.get(Task, task_id)
    if not task:
        raise ValueError("Task not found")
    if task.status not in {TaskStatus.queued, TaskStatus.running}:
        raise ValueError("Only queued or running tasks can be canceled")
    payload = task_to_payload(task)
    if task.type == "execute_proposal_batch" and payload.get("batch_id"):
        batch = session.get(ProposalBatch, payload["batch_id"])
        if batch and batch.status in {ProposalStatus.approved, ProposalStatus.executing}:
            batch.status = ProposalStatus.pending
            for item in batch.items:
                if item.status in {ProposalStatus.approved, ProposalStatus.executing}:
                    item.status = ProposalStatus.pending
    task.status = TaskStatus.canceled
    task.lease_until = None
    session.commit()
    session.refresh(task)
    return task
