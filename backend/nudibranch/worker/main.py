import asyncio
import json
import time

from sqlalchemy.orm import Session

from nudibranch.db.init import init_db
from nudibranch.db.models import ProposalBatch, ProposalItem, ProposalKind
from nudibranch.db.session import SessionLocal
from nudibranch.services.imports import discover_import_files
from nudibranch.services.notifications import create_notification, deliver_apns_notifications
from nudibranch.services.tasks import claim_next_task, complete_task, fail_task, task_to_payload


def run_propose_import(session: Session, payload: dict) -> dict:
    files = discover_import_files(payload.get("path"), include_fingerprint=True)
    batch = ProposalBatch(title="Import folder review", kind=ProposalKind.import_files, tree_path="/app/import")
    session.add(batch)
    session.flush()

    artist_items: dict[str, ProposalItem] = {}
    album_items: dict[tuple[str, str], ProposalItem] = {}

    for file_info in files:
        metadata = file_info["metadata"]
        artist = metadata.get("albumartist") or metadata.get("artist") or "Unknown Artist"
        album = metadata.get("album") or "Unknown Album"
        track_title = metadata.get("title") or file_info["relative_path"]
        artist_item = artist_items.get(artist)
        if not artist_item:
            artist_item = ProposalItem(
                batch_id=batch.id,
                title=artist,
                kind=ProposalKind.import_files,
                payload_json=json.dumps({"artist": artist}),
            )
            session.add(artist_item)
            session.flush()
            artist_items[artist] = artist_item

        album_key = (artist, album)
        album_item = album_items.get(album_key)
        if not album_item:
            album_item = ProposalItem(
                batch_id=batch.id,
                parent_id=artist_item.id,
                title=album,
                kind=ProposalKind.import_files,
                payload_json=json.dumps({"artist": artist, "album": album}),
            )
            session.add(album_item)
            session.flush()
            album_items[album_key] = album_item

        session.add(
            ProposalItem(
                batch_id=batch.id,
                parent_id=album_item.id,
                title=track_title,
                kind=ProposalKind.import_files,
                old_value=file_info["path"],
                new_value=file_info["suggested_library_path"],
                payload_json=json.dumps(file_info),
            )
        )
        session.add(
            ProposalItem(
                batch_id=batch.id,
                parent_id=album_item.id,
                title=f"Write metadata for {track_title}",
                kind=ProposalKind.metadata,
                old_value=file_info["relative_path"],
                new_value=json.dumps(metadata),
                payload_json=json.dumps(
                    {
                        "source_path": file_info["path"],
                        "size_bytes": file_info["size_bytes"],
                        "mtime_ns": file_info["mtime_ns"],
                        "metadata": metadata,
                    }
                ),
            )
        )
    create_notification(
        session,
        title="Import ready for approval",
        body=f"{len(files)} files were found in /app/import.",
        event_type="approval_needed",
        target_url="/approvals",
    )
    return {"batch_id": batch.id, "files": len(files)}


def run_execute_proposal_batch(session: Session, payload: dict) -> dict:
    batch_id = payload["batch_id"]
    batch = session.get(ProposalBatch, batch_id)
    if not batch:
        raise ValueError("Proposal batch not found")
    selected_items = [item for item in batch.items if item.selected]
    create_notification(
        session,
        title="Approved batch queued",
        body=f"{len(selected_items)} selected operations are queued for execution.",
        event_type="task_completed",
        target_url="/tasks",
    )
    return {"batch_id": batch_id, "selected_items": len(selected_items)}


def run_process_wishlist(session: Session, _payload: dict) -> dict:
    create_notification(
        session,
        title="Wishlist search finished",
        body="Download candidates are ready to review.",
        event_type="approval_needed",
        target_url="/approvals",
    )
    return {"status": "stubbed", "message": "slskd ranking pipeline placeholder created"}


TASK_HANDLERS = {
    "propose_import": run_propose_import,
    "execute_proposal_batch": run_execute_proposal_batch,
    "process_wishlist": run_process_wishlist,
}


async def worker_loop() -> None:
    with SessionLocal() as session:
        init_db(session)

    while True:
        with SessionLocal() as session:
            task = claim_next_task(session)
            if not task:
                await deliver_apns_notifications(session)
                time.sleep(2)
                continue

            try:
                handler = TASK_HANDLERS.get(task.type)
                if not handler:
                    raise ValueError(f"No handler registered for task type {task.type}")
                result = handler(session, task_to_payload(task))
                complete_task(session, task, result)
            except Exception as error:  # noqa: BLE001 - worker must persist task failures.
                fail_task(session, task, str(error))


if __name__ == "__main__":
    asyncio.run(worker_loop())
