import json
import os
import socket
import time
from datetime import datetime, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from nudibranch.core.config import get_settings
from nudibranch.db.models import ProposalBatch, ProposalStatus, Task, TaskStatus
from nudibranch.services.app_log import write_app_log


#: Task types the search lane runs, on its own thread beside the main lane. A candidate search is
#: almost all waiting on Soulseek, and one album can take a quarter of an hour. On a single lane it
#: held up every other search and the download scan behind it, so finished transfers sat unimported
#: and the one download slot sat idle (castiel, 2026-10-08). Searches only create pending candidate
#: batches and never touch the download manifest, which stays the main lane's alone.
SEARCH_LANE_TASK_TYPES = frozenset({"search_wishlist_item", "search_candidates", "search_alternatives"})


def task_wake_path():
    return get_settings().config_path / ".nudibranch-task-wake"


def task_wake_mark() -> int:
    """The wake file's mtime (ns), 0 when it does not exist. A change means a task was queued."""
    try:
        return task_wake_path().stat().st_mtime_ns
    except OSError:
        return 0


def touch_task_wake() -> None:
    """Tell the worker lanes (a different process) that a task was just queued. Best effort: a
    lane that misses it still finds the task on its next idle poll."""
    try:
        path = task_wake_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        os.utime(path, None)
    except OSError:
        pass


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
    touch_task_wake()
    return task


def task_to_payload(task: Task) -> dict:
    return json.loads(task.payload_json or "{}")


def task_result(task: Task) -> dict | None:
    if not task.result_json:
        return None
    return json.loads(task.result_json)


def claim_next_task(
    session: Session,
    lease_seconds: int = 300,
    *,
    only_types: frozenset[str] | None = None,
    exclude_types: frozenset[str] | None = None,
) -> Task | None:
    """Claim the oldest runnable task. The type filters split the queue between worker lanes, and
    the lanes' sets must partition it exactly, so no task is claimed by two lanes or by none."""
    worker_id = socket.gethostname()
    now = datetime.now(timezone.utc)
    query = select(Task).where(
        or_(
            Task.status == TaskStatus.queued,
            and_(Task.status == TaskStatus.running, Task.lease_until < now),
        )
    )
    if only_types is not None:
        query = query.where(Task.type.in_(only_types))
    if exclude_types is not None:
        query = query.where(Task.type.not_in(exclude_types))
    candidate = session.scalar(query.order_by(Task.created_at.asc()).limit(1))
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
    task.result_json = json.dumps(payload)
    task.lease_until = Task.lease_expiry(300)
    session.commit()


class ScanProgress:
    """Progress, a time-left estimate and cooperative cancel for a long library scan.

    Call `step()` at the top of every iteration and stop the loop when it returns False. The scan
    then finishes normally with whatever it has found so far: a cancel stops the scanning, it
    does not throw away findings.

    Writes are throttled, because each one takes SQLite's single write lock. The cancel check
    rides on the same throttle, so a cancel lands within about a second of work.
    `progress.cancelable` tells clients this task will actually honour Cancel while running.
    """

    MIN_WRITE_INTERVAL = 1.0
    # An estimate from the first couple of items is noise, so none is given until both pass.
    ETA_MIN_ITEMS = 3
    ETA_MIN_SECONDS = 5.0

    def __init__(self, session: Session, task: Task | None, total: int) -> None:
        self.session = session
        self.task = task
        self.total = max(1, total)
        self.done = 0
        self.canceled = False
        self._started = time.monotonic()
        self._last_write: float | None = None

    def step(self, message: str) -> bool:
        """Report that the next item is starting. False once the task has been cancelled."""
        if self.canceled:
            return False
        done = self.done
        self.done += 1
        if self.task is None:
            return True
        now = time.monotonic()
        if self._last_write is not None and now - self._last_write < self.MIN_WRITE_INTERVAL:
            return True
        self._last_write = now
        status = self.session.scalar(select(Task.status).where(Task.id == self.task.id))
        if status == TaskStatus.canceled:
            self.canceled = True
            return False
        elapsed = now - self._started
        eta_seconds = None
        if done >= self.ETA_MIN_ITEMS and elapsed >= self.ETA_MIN_SECONDS:
            eta_seconds = round(elapsed / done * (self.total - done))
        update_task_progress(
            self.session, self.task, min(done, self.total), self.total, message,
            eta_seconds=eta_seconds, cancelable=True,
        )
        return True

    def log_if_canceled(self, label: str) -> None:
        if self.canceled:
            append_task_log(
                self.session, self.task,
                f"{label} cancelled after {self.done - 1} of {self.total}; keeping what was found so far",
                "warning",
            )


def fail_task(session: Session, task: Task, error: str) -> None:
    task.status = TaskStatus.failed
    task.error = error
    task.lease_until = None
    session.commit()


def append_task_log(session: Session, task: Task | None, message: str, level: str = "info", **context: object) -> None:
    write_app_log(
        message,
        level=level,
        task_id=task.id if task else None,
        task_type=task.type if task else None,
        **context,
    )


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


def recover_orphaned_tasks(session: Session) -> int:
    """On worker startup, any task still marked running was orphaned by a crash/reboot
    (single-worker deployment). Requeue them so they resume. Attempts are preserved so the
    MAX_TASK_ATTEMPTS poison guard still trips for tasks that repeatedly crash the worker."""
    orphaned = list(session.scalars(select(Task).where(Task.status == TaskStatus.running)))
    for task in orphaned:
        task.status = TaskStatus.queued
        task.locked_by = None
        task.lease_until = None
        write_app_log(
            f"Recovered interrupted task {task.type} after restart; requeued to resume",
            level="warning",
            task_id=task.id,
            task_type=task.type,
        )
    if orphaned:
        session.commit()
    return len(orphaned)


def discard_pending_batches(session: Session, title: str, kind) -> int:
    """Delete prior proposal batches with the same title+kind that are still fully pending
    (not approved/executing/completed). Lets a re-run of a check/scan tool replace its previous
    un-acted proposal instead of piling up duplicates — important when a crashed scan resumes."""
    stale = list(
        session.scalars(
            select(ProposalBatch).where(
                ProposalBatch.title == title,
                ProposalBatch.kind == kind,
                ProposalBatch.status == ProposalStatus.pending,
            )
        )
    )
    for batch in stale:
        session.delete(batch)  # items cascade
    if stale:
        session.flush()
    return len(stale)
