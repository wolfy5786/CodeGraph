"""Graph workspace CRUD — POST/GET/DELETE /api/v1/graphs."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status

from app.falkor import FalkorClient
from app.models import CreateGraphRequest, GraphListResponse, GraphStats

logger = logging.getLogger(__name__)
_SERVICE = "api.graphs"

router = APIRouter(tags=["graphs"])


def _falkor(request: Request) -> FalkorClient:
    return request.app.state.falkor


@router.post("/graphs", status_code=status.HTTP_201_CREATED, response_model=GraphStats)
async def create_graph(body: CreateGraphRequest, request: Request) -> GraphStats:
    falkor = _falkor(request)
    name = body.name.strip()
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="name must not be empty",
        )
    if falkor.graph_exists(name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Graph '{name}' already exists",
        )
    try:
        falkor.create_graph(name)
    except Exception as err:
        logger.error(
            "Failed to create graph",
            extra={"service": _SERVICE, "graph": name, "error": str(err)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create graph",
        ) from err
    logger.info("Graph workspace created", extra={"service": _SERVICE, "graph": name})
    return GraphStats(name=name, repo_count=0, node_count=1, edge_count=0)


@router.get("/graphs", response_model=GraphListResponse)
async def list_graphs(request: Request) -> GraphListResponse:
    falkor = _falkor(request)
    try:
        names = falkor.list_graphs()
    except Exception as err:
        logger.error("Failed to list graphs", extra={"service": _SERVICE, "error": str(err)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list graphs",
        ) from err
    return GraphListResponse(graphs=names)


@router.get("/graphs/{name}", response_model=GraphStats)
async def get_graph(name: str, request: Request) -> GraphStats:
    falkor = _falkor(request)
    if not falkor.graph_exists(name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Graph '{name}' not found",
        )
    try:
        stats = falkor.get_graph_stats(name)
    except Exception as err:
        logger.error(
            "Failed to fetch graph stats",
            extra={"service": _SERVICE, "graph": name, "error": str(err)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch graph stats",
        ) from err
    return GraphStats(name=name, **stats)


@router.delete("/graphs/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_graph(name: str, request: Request) -> None:
    falkor = _falkor(request)
    if not falkor.graph_exists(name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Graph '{name}' not found",
        )
    try:
        falkor.delete_graph(name)
    except Exception as err:
        logger.error(
            "Failed to delete graph",
            extra={"service": _SERVICE, "graph": name, "error": str(err)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete graph",
        ) from err
    logger.info("Graph workspace deleted", extra={"service": _SERVICE, "graph": name})
