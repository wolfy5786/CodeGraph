# CodeGraph — Repository Update System Design

> Covers the three update operations (`check_update`, `update`, `recreate`), the async request queue, combining rules, per-repo state snapshots, read locking, and long-polling delivery. Post-dequeue processing logic for `check_update` and `update` is **TBD** and intentionally excluded.

---

## 1. Update Request Types

### 1.1 `check_update`

Instructs the system to auto-detect what has changed in the repository since the last ingestion. The system is responsible for discovering the delta; the caller does not supply file lists.

- **When used**: caller knows the repo may have changed but does not know which files.
- **Post-dequeue behavior**: **TBD** (compare on-disk SHA-256 of each tracked file against the `content_hash` stored on the corresponding `:File` node in FalkorDB to produce a dirty-file set; then apply incremental graph reconciliation).

### 1.2 `update`

Caller supplies an explicit manifest of file-system changes. The system applies those deltas to the existing graph without a full re-crawl.

- **When used**: caller (typically the file watcher) already knows exactly which paths changed.
- **Request body**:

```json
{
  "created": ["src/auth/token.py", "src/auth/__init__.py"],
  "updated": ["src/core/graph.py"],
  "deleted": ["src/legacy/old_parser.py"]
}
```

- **Post-dequeue behavior**: **TBD** (incremental graph reconciliation).

### 1.3 `recreate`

Tears down the repository's named graph and runs a full Phase 0 + Phase 1 + Phase 2 ingestion from scratch. Semantically equivalent to `DELETE repo` → `POST ingest`.

- **When used**: structural change too large for incremental update, or graph corruption suspected.
- **Post-dequeue behavior**: clear the repository's named graph, then invoke the standard three-phase ingestion pipeline.

---

## 2. Input Surfaces & Permissions

| Surface | `check_update` | `update` (delta manifest) | `recreate` |
|---------|:--------------:|:-------------------------:|:----------:|
| **CLI** | ✓ | ✓ | ✓ |
| **REST** | ✓ | — | ✓ |
| **MCP** | ✓ | — | ✓ |
| **File watcher** (internal) | — | ✓ | — |

The file watcher is an internal component and never issues `check_update` or `recreate`; it always knows which files changed. MCP and REST cannot supply file manifests — they can only trigger auto-detection or full recreation.

---

## 3. REST API Endpoints

All update endpoints are non-blocking: they enqueue a job and return `202 Accepted` immediately. Progress is observable via the status endpoint or the long-poll endpoint.

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/graphs/{name}/repositories/{repo}/check-update` | Enqueue a `check_update` request |
| `POST` | `/api/v1/graphs/{name}/repositories/{repo}/update` | Enqueue an `update` request with delta manifest |
| `POST` | `/api/v1/graphs/{name}/repositories/{repo}/recreate` | Enqueue a `recreate` request |
| `GET` | `/api/v1/graphs/{name}/repositories/{repo}/status` | Current job state (existing endpoint, extended with update states) |
| `GET` | `/api/v1/graphs/{name}/repositories/{repo}/updates/poll` | Long-poll: blocks until the current update cycle completes, then delivers the response |

### `POST .../update` body

```json
{
  "created": ["<relative-path>", "..."],
  "updated": ["<relative-path>", "..."],
  "deleted": ["<relative-path>", "..."]
}
```

All paths are relative to the repository root registered at ingestion time. All three keys are optional; an empty manifest is a no-op.

### `202 Accepted` response (all enqueue endpoints)

```json
{
  "repo_id": "<repo>",
  "queued_as": "check_update | update | recreate",
  "position": 1
}
```

`position` reflects queue depth at enqueue time and is informational only.

---

## 4. Async Request Queue

### 4.1 Queue mechanism

The queue is implemented as **per-repo job slots over asyncio primitives** — no external broker (no Redis queue, no Celery, no ARQ).

**Why not a traditional queue backend:**  
The combining rules in §4.2 require reading and conditionally mutating the pending entry atomically. Off-the-shelf queues (FIFO `asyncio.Queue`, Redis list) do not support in-place mutation of waiting items. More fundamentally, the combining rules reduce maximum queue depth per repo to **1** — there is never more than one pending entry per repo after collapsing. This is a single pending slot with replace/merge semantics, not a queue.

**Per-repo slot structure (`RepoSlot`):**

```python
@dataclass
class RepoSlot:
    pending: Optional[Job]    # at most 1 pending job after combining; None if idle
    slot_lock: asyncio.Lock   # protects `pending` during combine writes (enqueue path)
    write_lock: asyncio.Lock  # held during graph mutation — exclusive write, reads served from snapshot
    wakeup: asyncio.Event     # set when a job is enqueued; wakes the per-repo worker coroutine
    completion: asyncio.Event # set when mutation finishes; unblocks long-poll subscribers (§6)
```

One `RepoSlot` is created lazily on first enqueue for a repo and lives for the daemon lifetime. Slots for different repos are fully independent; their worker coroutines run concurrently.

**Enqueue path (inside an API handler):**

1. Acquire `slot_lock`.
2. Apply combining rule (§4.2) against current `pending` — merge, replace, or discard.
3. Write the new (or merged) `pending`; preserve earliest arrival timestamp if collapsing.
4. Release `slot_lock`; fire `wakeup`.
5. Return `202` with `position` (0 = currently processing, 1 = waiting in slot).

**Worker coroutine (one per repo, started on first enqueue):**

1. `await wakeup` — blocks until a job arrives.
2. Acquire `slot_lock`; atomically take `pending` (set to `None`); release `slot_lock`.
3. Acquire `write_lock`; save snapshot (§5.1).
4. Execute the job (`recreate` pipeline, or `check_update`/`update` TBD).
5. Release `write_lock`; delete snapshot; fire `completion`.
6. Loop back to step 1.

**Long-poll integration (§6):**

```python
await asyncio.wait_for(slot.completion.wait(), timeout=timeout_seconds)
```

The `completion` event is cleared at the start of each new job and set on finish, so late-arriving poll requests that miss a completion cycle simply wait for the next one.

There is **one logical queue per repo ID**. Requests for different repos are independent and may be processed concurrently.

### 4.2 Combining rules (pre-dequeue only)

Combining applies **only while a request is still waiting in the queue**. Once a request has been dequeued and processing has begun it is no longer a candidate for combining.

| Situation | Outcome |
|-----------|---------|
| A new `update` arrives and only `update` items exist for this repo in the queue | Merge all delta manifests into a single `update` entry (union of `created`, `updated`, `deleted` sets) |
| A `check_update` arrives for a repo that has pending `update` items | Discard all pending `update` items; keep only the new `check_update` |
| A `recreate` arrives for a repo that has any pending items (`update` or `check_update`) | Discard all pending items for that repo; keep only the new `recreate` |
| A second `check_update` arrives and a `check_update` already exists in the queue | Deduplicate; one `check_update` remains |
| A second `recreate` arrives and a `recreate` already exists in the queue | Deduplicate; one `recreate` remains |

**Priority order** (highest wins on collapse): `recreate` > `check_update` > `update`.

Any queue entry that results from collapsing multiple incoming requests retains the **arrival timestamp of the earliest contributing request** for ordering purposes.

### 4.3 Dequeue behavior

When the worker picks up the next entry for a repo:

1. The entry is **atomically removed** from the queue.
2. From this point the entry is invisible to the combining logic — subsequent arrivals for the same repo start a fresh queue entry.
3. The worker proceeds to the state-snapshot and lock step (§ 5).

---

## 5. Repo State & Concurrency

### 5.1 State snapshot on dequeue

Immediately after dequeuing, before any graph mutation begins, the system saves a **snapshot of the current repo graph state** (exact serialization format TBD — could be a Falkor dump, a node-edge export, or a version pointer). This snapshot is the "last known good state" that reads will serve during the update.

### 5.2 Read lock

After the snapshot is saved, the system acquires a **per-repo read lock** (semaphore/flag) for the duration of the update operation.

- The lock is **exclusive to writes** only: multiple concurrent read requests are all served from the snapshot simultaneously.
- No write or second update operation may begin on the same repo while the lock is held.
- If a new update request arrives for a locked repo it enters the queue normally and waits.

### 5.3 Read request behavior during lock

When a read request (query, status, graph traversal) arrives for a repo that is currently locked:

1. The response is served from the **saved snapshot**, not the live graph.
2. The response includes a metadata field indicating the update is in progress:

```json
{
  "data": { ... },
  "_meta": {
    "update_in_progress": true,
    "snapshot_taken_at": "2026-06-03T14:22:00Z",
    "message": "Graph update is underway. Results reflect the last stable state."
  }
}
```

### 5.4 Lock release & cleanup

When the update operation completes (success or terminal failure):

1. The read lock is released.
2. The saved snapshot is deleted.
3. Any long-poll subscribers are notified (§ 6).

On **failure**, the snapshot may optionally be promoted back as the live graph if the update left the graph in a partially mutated state — this recovery strategy is **TBD**.

---

## 6. Long Polling

### 6.1 Endpoint

```
GET /api/v1/graphs/{name}/repositories/{repo}/updates/poll
```

Optional query parameters:

| Parameter | Description |
|-----------|-------------|
| `timeout` | Max seconds to wait before returning a `408 Request Timeout` (default: 60, max: 300) |
| `since` | ISO-8601 timestamp; only resolves if an update completed *after* this time |

### 6.2 Behavior

1. If **no update is in progress** and no recent completion matches `since`: respond immediately with the current graph state (no blocking).
2. If an **update is in progress**: hold the connection open until the lock is released, then deliver the updated graph state (or a pointer to it) in a single response.
3. On timeout: return `408` with the last snapshot state and `"update_in_progress": true`.

### 6.3 Response on completion

```json
{
  "repo_id": "<repo>",
  "completed_at": "2026-06-03T14:25:10Z",
  "outcome": "success | failed",
  "data": { ... }
}
```

Callers should treat `"outcome": "failed"` as a signal to re-query status for error details rather than assuming the data is current.

---

## 7. `recreate` Processing

`recreate` is the only operation whose post-dequeue processing is fully defined.

1. Acquire the read lock and save the snapshot (§ 5.1–5.2).
2. Clear the repository's named graph (it holds only this repo, so a graph wipe is sufficient).
3. Invoke the standard three-phase ingestion pipeline (Phase 0 filesystem scan + structural graph write → Phase 1 SCIP extraction → Phase 2 SCIP labels + regex + SCIP relationships) exactly as `POST .../ingest` does.
4. On completion, release the lock and delete the snapshot (§ 5.4).

State machine extension for the existing `status` endpoint:

```
queued → snapshot → recreating → phase0 → phase1 → phase2 → done | failed
```

---

## 8. `check_update` and `update` Processing — TBD

Post-dequeue logic for these two operations (diff strategy, incremental graph reconciliation, re-embedding) is deferred. The queue, locking, and snapshot contracts defined above apply regardless.

---

## 9. Interaction with Existing Endpoints

| Existing endpoint | Behavior during update lock |
|------------------|-----------------------------|
| `GET .../status` | Returns current update state + snapshot timestamp |
| `POST .../ingest` | Rejected with `409 Conflict` while a lock is held |
| `DELETE .../repositories/{repo}` | Rejected with `409 Conflict` while a lock is held |
| `POST .../query` (TBD) | Served from snapshot with `update_in_progress` metadata |

---

## Related Docs

- REST contract (existing endpoints): [Backend_API.md](Backend_API.md)
- Ingestion pipeline: [PHASE0_IMPLEMENTATION.md](PHASE0_IMPLEMENTATION.md), [PHASE1_IMPLEMENTATION.md](PHASE1_IMPLEMENTATION.md), [PHASE2_IMPLEMENTATION.md](PHASE2_IMPLEMENTATION.md)
- Architecture & surfaces: [DESKTOP_ARCHITECTURE.md](DESKTOP_ARCHITECTURE.md)
