"""Pydantic request / response models for the CodeGraph REST API."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Graphs
# ---------------------------------------------------------------------------


class CreateGraphRequest(BaseModel):
    name: str


class GraphStats(BaseModel):
    name: str
    repo_count: int
    node_count: int
    edge_count: int


class GraphListResponse(BaseModel):
    graphs: list[str]


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------


class AttachRepoRequest(BaseModel):
    name: str
    local_path: str


class RepoInfo(BaseModel):
    name: str
    local_path: str
    last_ingested_at: Optional[str] = None


class RepoListResponse(BaseModel):
    repositories: list[RepoInfo]


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


class IngestRequest(BaseModel):
    local_path: str
    options: dict = Field(default_factory=dict)


class IngestAccepted(BaseModel):
    job_id: str
    status: str = "queued"


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    repo: str
    graph: str
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str
    falkordb: str


class VersionResponse(BaseModel):
    version: str
