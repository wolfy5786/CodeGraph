"""Repository registration, ingest stub, and status — /api/v1/graphs/{name}/repositories."""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, HTTPException, Request, status

from app import job_store
from app.falkor import FalkorClient
from app.models import (
    AttachRepoRequest,
    IngestAccepted,
    IngestRequest,
    JobStatusResponse,
    RepoInfo,
    RepoListResponse,
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
# Ingest (stub — returns 202 + job ID; no worker is dispatched yet)
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

    job_id = await job_store.create_job(graph=name, repo=repo)
    logger.info(
        "Ingest job accepted (stub — worker not yet wired)",
        extra={"service": _SERVICE, "graph": name, "repo": repo, "job_id": job_id},
    )
    return IngestAccepted(job_id=job_id, status="queued")


@router.get(
    "/graphs/{name}/repositories/{repo}/status",
    response_model=JobStatusResponse,
)
async def get_ingest_status(name: str, repo: str, request: Request) -> JobStatusResponse:
    falkor = _falkor(request)
    _require_graph(falkor, name)
    _require_repo(falkor, name, repo)

    job = await job_store.get_job(graph=name, repo=repo)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No ingest job found for '{repo}' in graph '{name}'",
        )
    return JobStatusResponse(**job)
