"""Repository registration, ingest, status, and update endpoints — /api/v1/graphs/{name}/repositories."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, status

from app import ingestion_service, job_store, update_store
from app.falkor import FalkorClient
from app.models import (
    AttachRepoRequest,
    IngestAccepted,
    IngestRequest,
    JobStatusResponse,
    PollResponse,
    RepoInfo,
    RepoListResponse,
    UpdateAccepted,
    UpdateRequest,
    UpdateStatus,
)

logger = logging.getLogger(__name__)
_SERVICE = "api.repositories"

router = APIRouter(tags=["repositories"])


def _falkor(request: Request) -> FalkorClient:
    return request.app.state.falkor


def _require_graph(falkor: FalkorClient, name: str) -> None:
    if not falkor.graph_exists(name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Graph '{name}' not found",
        )


def _require_repo(falkor: FalkorClient, graph: str, repo: str) -> None:
    if not falkor.repo_exists(graph, repo):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Repository '{repo}' not found in graph '{graph}'",
        )


async def _check_write_lock(graph: str, repo: str) -> None:
    """Raise 409 if an update write lock is currently held for this repo (§9)."""
    slot = await update_store.get_slot(graph, repo)
    if slot is not None and slot.write_lock.locked():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Repository '{repo}' has an update in progress; retry after it completes",
        )


# ---------------------------------------------------------------------------
# Repository CRUD
# ---------------------------------------------------------------------------


@router.post(
    "/graphs/{name}/repositories",
    status_code=status.HTTP_201_CREATED,
    response_model=RepoInfo,
)
async def attach_repo(name: str, body: AttachRepoRequest, request: Request) -> RepoInfo:
    falkor = _falkor(request)
    _require_graph(falkor, name)

    if not os.path.isdir(body.local_path):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"local_path '{body.local_path}' is not a readable directory on this machine",
        )

    if falkor.repo_exists(name, body.name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Repository '{body.name}' is already attached to graph '{name}'",
        )

    try:
        falkor.attach_repo(name, body.name, body.local_path)
    except Exception as err:
        logger.error(
            "Failed to attach repository",
            extra={"service": _SERVICE, "graph": name, "repo": body.name, "error": str(err)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to attach repository",
        ) from err

    logger.info("Repository attached", extra={"service": _SERVICE, "graph": name, "repo": body.name})
    return RepoInfo(name=body.name, local_path=body.local_path)


@router.get("/graphs/{name}/repositories", response_model=RepoListResponse)
async def list_repos(name: str, request: Request) -> RepoListResponse:
    falkor = _falkor(request)
    _require_graph(falkor, name)

    try:
        raw = falkor.list_repos(name)
    except Exception as err:
        logger.error(
            "Failed to list repositories",
            extra={"service": _SERVICE, "graph": name, "error": str(err)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list repositories",
        ) from err

    return RepoListResponse(repositories=[RepoInfo(**r) for r in raw])


@router.delete(
    "/graphs/{name}/repositories/{repo}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_repo(name: str, repo: str, request: Request) -> None:
    falkor = _falkor(request)
    _require_graph(falkor, name)
    _require_repo(falkor, name, repo)
    await _check_write_lock(name, repo)

    try:
        falkor.delete_repo(name, repo)
    except Exception as err:
        logger.error(
            "Failed to delete repository",
            extra={"service": _SERVICE, "graph": name, "repo": repo, "error": str(err)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete repository",
        ) from err

    logger.info("Repository deleted", extra={"service": _SERVICE, "graph": name, "repo": repo})


# ---------------------------------------------------------------------------
# Ingest — Phase 0 filesystem scan + structural graph write
# ---------------------------------------------------------------------------


@router.post(
    "/graphs/{name}/repositories/{repo}/ingest",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=IngestAccepted,
)
async def trigger_ingest(
    name: str, repo: str, body: IngestRequest, request: Request
) -> IngestAccepted:
    falkor = _falkor(request)
    _require_graph(falkor, name)
    _require_repo(falkor, name, repo)
    await _check_write_lock(name, repo)

    # Reject if a job is already queued or running for this repo.
    existing = await job_store.get_job(graph=name, repo=repo)
    if existing and existing.get("status") not in ("done", "failed"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Ingest job '{existing['job_id']}' is already "
                f"{existing['status']} for '{repo}' in graph '{name}'"
            ),
        )

    local_path = falkor.get_repo_local_path(name, repo)
    if local_path is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Repository '{repo}' has no local_path registered in graph '{name}'",
        )

    job_id = await job_store.create_job(graph=name, repo=repo)

    asyncio.create_task(
        ingestion_service.run_ingest(
            graph=name,
            repo=repo,
            local_path=local_path,
            job_id=job_id,
        ),
        name=f"ingest:{name}:{repo}:{job_id}",
    )

    logger.info(
        "Ingest job accepted",
        extra={"service": _SERVICE, "graph": name, "repo": repo, "job_id": job_id},
    )
    return IngestAccepted(job_id=job_id, status="queued")


@router.get(
    "/graphs/{name}/repositories/{repo}/status",
    response_model=JobStatusResponse,
)
async def get_repo_status(name: str, repo: str, request: Request) -> JobStatusResponse:
    falkor = _falkor(request)
    _require_graph(falkor, name)
    _require_repo(falkor, name, repo)

    job = await job_store.get_job(graph=name, repo=repo)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No ingest job found for '{repo}' in graph '{name}'",
        )

    slot = await update_store.get_slot(graph=name, repo=repo)
    update_info: Optional[UpdateStatus] = None
    if slot is not None and slot.state not in ("idle", "done"):
        update_info = UpdateStatus(
            job_type=slot.current_job_type or "unknown",
            state=slot.state,
            update_in_progress=slot.processing,
        )

    return JobStatusResponse(**job, update_info=update_info)


# ---------------------------------------------------------------------------
# Update operations — check_update, update, recreate, long-poll (§3)
# ---------------------------------------------------------------------------


@router.post(
    "/graphs/{name}/repositories/{repo}/check-update",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=UpdateAccepted,
)
async def check_update(name: str, repo: str, request: Request) -> UpdateAccepted:
    falkor = _falkor(request)
    _require_graph(falkor, name)
    _require_repo(falkor, name, repo)

    position = await update_store.enqueue(
        graph=name, repo=repo, job_type=update_store.JobType.check_update
    )
    logger.info(
        "check_update accepted (stub)",
        extra={"service": _SERVICE, "graph": name, "repo": repo, "position": position},
    )
    return UpdateAccepted(repo_id=repo, queued_as="check_update", position=position)


@router.post(
    "/graphs/{name}/repositories/{repo}/update",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=UpdateAccepted,
)
async def update_repo(
    name: str, repo: str, body: UpdateRequest, request: Request
) -> UpdateAccepted:
    falkor = _falkor(request)
    _require_graph(falkor, name)
    _require_repo(falkor, name, repo)

    position = await update_store.enqueue(
        graph=name,
        repo=repo,
        job_type=update_store.JobType.update,
        created=body.created,
        updated_paths=body.updated,
        deleted=body.deleted,
    )
    logger.info(
        "update accepted (stub)",
        extra={
            "service": _SERVICE,
            "graph": name,
            "repo": repo,
            "position": position,
            "created_count": len(body.created),
            "updated_count": len(body.updated),
            "deleted_count": len(body.deleted),
        },
    )
    return UpdateAccepted(repo_id=repo, queued_as="update", position=position)


@router.post(
    "/graphs/{name}/repositories/{repo}/recreate",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=UpdateAccepted,
)
async def recreate_repo(name: str, repo: str, request: Request) -> UpdateAccepted:
    falkor = _falkor(request)
    _require_graph(falkor, name)
    _require_repo(falkor, name, repo)

    position = await update_store.enqueue(
        graph=name, repo=repo, job_type=update_store.JobType.recreate
    )
    logger.info(
        "recreate accepted (stub)",
        extra={"service": _SERVICE, "graph": name, "repo": repo, "position": position},
    )
    return UpdateAccepted(repo_id=repo, queued_as="recreate", position=position)


@router.get(
    "/graphs/{name}/repositories/{repo}/updates/poll",
    response_model=PollResponse,
)
async def poll_update(
    name: str,
    repo: str,
    request: Request,
    timeout: int = Query(default=60, ge=1, le=300),
    since: Optional[str] = Query(default=None),
) -> PollResponse:
    falkor = _falkor(request)
    _require_graph(falkor, name)
    _require_repo(falkor, name, repo)

    slot = await update_store.get_slot(graph=name, repo=repo)

    # Validate `since` early if provided.
    since_dt: Optional[datetime] = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="`since` must be an ISO-8601 timestamp",
            )

    # Determine whether to block or resolve immediately.
    in_flight = slot is not None and (slot.processing or slot.pending is not None)
    last_completion = slot.last_completed_at if slot else None

    # If `since` is given and last completion predates it, treat as still-waiting.
    if not in_flight and since_dt is not None:
        if last_completion is None or last_completion <= since_dt:
            in_flight = True

    if not in_flight:
        completed_at = last_completion or datetime.now(timezone.utc)
        return PollResponse(
            repo_id=repo,
            completed_at=completed_at.isoformat(),
            outcome="success",
            data={},
        )

    # Block until the current update cycle completes.
    logger.info(
        "Long-poll subscriber waiting",
        extra={"service": _SERVICE, "graph": name, "repo": repo, "timeout_s": timeout},
    )
    try:
        await asyncio.wait_for(slot.completion.wait(), timeout=float(timeout))
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_408_REQUEST_TIMEOUT,
            detail="Update did not complete within the requested timeout",
        )

    completed_at = slot.last_completed_at or datetime.now(timezone.utc)
    logger.info(
        "Long-poll resolved",
        extra={"service": _SERVICE, "graph": name, "repo": repo},
    )
    return PollResponse(
        repo_id=repo,
        completed_at=completed_at.isoformat(),
        outcome="success",
        data={},
    )
