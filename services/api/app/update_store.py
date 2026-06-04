"""Per-repo async update slot store.

Implements the RepoSlot structure, combining rules (§4.2), and per-repo
worker coroutines (§4.1) from UPDATE_DESIGN.md.  Worker bodies are stubs;
they signal completion without performing graph mutations.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)
_SERVICE = "api.update_store"

# Per-repo slot registry; created lazily on first enqueue.
_slots: dict[str, "RepoSlot"] = {}
_registry_lock = asyncio.Lock()


class JobType(str, Enum):
    check_update = "check_update"
    update = "update"
    recreate = "recreate"


# Priority used by combining rules — higher value wins on collapse (§4.2).
_PRIORITY: dict[JobType, int] = {
    JobType.recreate: 3,
    JobType.check_update: 2,
    JobType.update: 1,
}


@dataclass
class Job:
    type: JobType
    created: list[str] = field(default_factory=list)
    updated_paths: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    arrived_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class RepoSlot:
    """Synchronisation primitives and the single pending job for one repo (§4.1)."""

    pending: Optional[Job] = None
    slot_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    completion: asyncio.Event = field(default_factory=asyncio.Event)
    # Observability — read by status / poll endpoints; written only by the worker.
    processing: bool = False
    current_job_type: Optional[JobType] = None
    state: str = "idle"  # idle | queued | snapshot | recreating | processing | done | failed
    last_completed_at: Optional[datetime] = None


def _slot_key(graph: str, repo: str) -> str:
    return f"{graph}\x00{repo}"


def _combine(existing: Optional[Job], incoming: Job) -> Job:
    """Apply combining rules from §4.2 of UPDATE_DESIGN.md."""
    if existing is None:
        return incoming

    ep = _PRIORITY[existing.type]
    ip = _PRIORITY[incoming.type]

    if ip > ep:
        # Higher-priority incoming type wins; retain the earliest arrival timestamp.
        return Job(
            type=incoming.type,
            created=incoming.created,
            updated_paths=incoming.updated_paths,
            deleted=incoming.deleted,
            arrived_at=existing.arrived_at,
        )

    if existing.type == incoming.type == JobType.update:
        # Merge path manifests — union of all three sets (§4.2 row 1).
        return Job(
            type=JobType.update,
            created=list(set(existing.created) | set(incoming.created)),
            updated_paths=list(set(existing.updated_paths) | set(incoming.updated_paths)),
            deleted=list(set(existing.deleted) | set(incoming.deleted)),
            arrived_at=existing.arrived_at,
        )

    # Same non-update type (check_update+check_update, recreate+recreate) — deduplicate.
    return Job(type=existing.type, arrived_at=existing.arrived_at)


async def _worker(graph: str, repo: str, slot: RepoSlot) -> None:
    """Per-repo worker coroutine.  Stub: wakes on enqueue, signals completion immediately."""
    while True:
        await slot.wakeup.wait()
        slot.wakeup.clear()

        async with slot.slot_lock:
            job = slot.pending
            slot.pending = None

        if job is None:
            continue

        slot.completion.clear()
        slot.processing = True
        slot.current_job_type = job.type
        slot.state = "snapshot"

        logger.info(
            "Update worker dequeued job (stub — no graph mutation performed)",
            extra={
                "service": _SERVICE,
                "graph": graph,
                "repo": repo,
                "job_type": job.type,
                "arrived_at": job.arrived_at.isoformat(),
            },
        )

        async with slot.write_lock:
            # TODO: dispatch recreate pipeline (§7) or check_update / update logic (§8).
            slot.state = "recreating" if job.type == JobType.recreate else "processing"
            await asyncio.sleep(0)  # yield so other coroutines can run

        slot.processing = False
        slot.current_job_type = None
        slot.state = "done"
        slot.last_completed_at = datetime.now(timezone.utc)
        slot.completion.set()

        logger.info(
            "Update worker finished (stub)",
            extra={"service": _SERVICE, "graph": graph, "repo": repo, "job_type": job.type},
        )


async def _get_or_create_slot(graph: str, repo: str) -> RepoSlot:
    """Return the RepoSlot for (graph, repo), creating it and starting a worker if needed."""
    key = _slot_key(graph, repo)
    async with _registry_lock:
        if key not in _slots:
            slot = RepoSlot()
            _slots[key] = slot
            asyncio.create_task(
                _worker(graph, repo, slot), name=f"update-worker:{key}"
            )
            logger.info(
                "Created update slot and started worker coroutine",
                extra={"service": _SERVICE, "graph": graph, "repo": repo},
            )
        return _slots[key]


async def enqueue(
    graph: str,
    repo: str,
    job_type: JobType,
    created: Optional[list[str]] = None,
    updated_paths: Optional[list[str]] = None,
    deleted: Optional[list[str]] = None,
) -> int:
    """Enqueue an update job applying combining rules (§4.2).

    Returns the position at enqueue time:
        0 — a job is currently being processed (new request is pending in slot)
        1 — worker is idle (new request is waiting in slot)
    """
    slot = await _get_or_create_slot(graph, repo)
    incoming = Job(
        type=job_type,
        created=created or [],
        updated_paths=updated_paths or [],
        deleted=deleted or [],
    )

    async with slot.slot_lock:
        slot.pending = _combine(slot.pending, incoming)

    if not slot.processing:
        slot.state = "queued"

    slot.wakeup.set()
    position = 0 if slot.processing else 1

    logger.info(
        "Update job enqueued",
        extra={
            "service": _SERVICE,
            "graph": graph,
            "repo": repo,
            "job_type": job_type,
            "position": position,
        },
    )
    return position


async def get_slot(graph: str, repo: str) -> Optional[RepoSlot]:
    """Return the existing RepoSlot for (graph, repo), or None if never enqueued."""
    key = _slot_key(graph, repo)
    async with _registry_lock:
        return _slots.get(key)


async def get_or_create_slot(graph: str, repo: str) -> RepoSlot:
    """Return (or create) the RepoSlot for (graph, repo).

    Creating the slot also starts the per-repo update worker coroutine.
    Used by IngestionService to acquire the shared write_lock without
    going through the update job queue.
    """
    return await _get_or_create_slot(graph, repo)
