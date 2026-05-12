# Race Condition Rules

The Compose stack intentionally avoids requiring a separate broker. SQLite is
acceptable if all writes are designed carefully.

## Database

- SQLite runs in WAL mode.
- Connections set `busy_timeout=5000`.
- Foreign keys are enabled.
- The API creates tasks; the worker executes tasks.
- The worker claims tasks with an atomic conditional update.
- Expired leases can be reclaimed.

## File Operations

File mutations must be task-driven. API routes may create proposals and tasks,
but should not directly rename, move, tag, delete, or download files.

Execution tasks should:

1. Re-read proposal state before acting.
2. Verify the item is still selected and approved.
3. Acquire a per-path operation lock.
4. Write into a temporary path when possible.
5. Move atomically into the final path.
6. Update SQLite after the file operation succeeds.
7. Notify Jellyfin after Nudibranch state is committed.

## Backups

Backups must not run while file operations are active. A backup task should wait
for active file-operation leases to finish or fail clearly with a retryable
status.

## Import Folder

Files in `/app/import` may change while a scan is running. Import proposal tasks
should record file size and mtime. Execution must verify those values before
moving anything.

## Downloads

Downloads must land in `/app/downloads` first and only move to staging after the
download process exits and the final file size is stable.

