"""In-memory ingest job state store.

Keyed by (graph_name, repo_name). A real implementation would persist to
Redis or a sqlite sidecar; this stub is intentionally ephemeral.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Optional

_store: dict[str, dict] = {}
_lock: asyncio.Lock = asyncio.Lock()


def _key(graph: str, repo: str) -> str:
    return f"{graph}\x00{repo}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def create_job(graph: str, repo: str) -> str:
    """Record a new queued job and return its UUID."""
    job_id = str(uuid.uuid4())
    ts = _now()
    async with _lock:
        _store[_key(graph, repo)] = {
            "job_id": job_id,
            "status": "queued",
            "graph": graph,
            "repo": repo,
            "created_at": ts,
            "updated_at": ts,
        }
    return job_id


async def get_job(graph: str, repo: str) -> Optional[dict]:
    """Return the most recent job entry for (graph, repo), or None."""
    async with _lock:
        return _store.get(_key(graph, repo))
