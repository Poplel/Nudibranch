from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from nudibranch.db.models import ProposalBatch, ProposalItem, ProposalStatus, Task
from nudibranch.services.tasks import enqueue_task


def list_batches(session: Session) -> list[ProposalBatch]:
    return list(session.scalars(select(ProposalBatch).order_by(ProposalBatch.created_at.desc())))


def set_selection(session: Session, batch_id: str, item_ids: list[str], selected: bool) -> int:
    items = list(
        session.scalars(
            select(ProposalItem).where(ProposalItem.batch_id == batch_id, ProposalItem.id.in_(item_ids))
        )
    )
    for item in items:
        item.selected = selected
    session.commit()
    return len(items)


def approve_batch(session: Session, batch_id: str, item_ids: list[str] | None = None) -> Task:
    batch = session.get(ProposalBatch, batch_id)
    if not batch:
        raise ValueError("Proposal batch not found")
    batch.status = ProposalStatus.approved
    for item in batch.items:
        if item_ids is not None and item.id not in item_ids:
            continue
        if item.selected and item.status in {ProposalStatus.pending, ProposalStatus.failed}:
            item.status = ProposalStatus.approved
    session.commit()
    return enqueue_task(session, "execute_proposal_batch", {"batch_id": batch_id})


def reject_items(session: Session, batch_id: str, item_ids: list[str] | None, suppress_for: str) -> int:
    query = select(ProposalItem).where(ProposalItem.batch_id == batch_id)
    if item_ids:
        query = query.where(ProposalItem.id.in_(item_ids))

    suppress_until = None
    now = datetime.now(timezone.utc)
    if suppress_for == "day":
        suppress_until = now + timedelta(days=1)
    elif suppress_for == "week":
        suppress_until = now + timedelta(weeks=1)
    elif suppress_for == "forever":
        suppress_until = datetime.max.replace(tzinfo=timezone.utc)

    items = list(session.scalars(query))
    for item in items:
        item.status = ProposalStatus.rejected
        item.suppress_until = suppress_until

    batch = session.get(ProposalBatch, batch_id)
    if batch and all(item.status == ProposalStatus.rejected for item in batch.items):
        batch.status = ProposalStatus.rejected
    session.commit()
    return len(items)
