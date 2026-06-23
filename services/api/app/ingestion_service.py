"""Create-path ingestion service.

Runs Phase 0 (filesystem scan + structural graph write) for a registered
repository.

Thread model
------------
- Scanner.scan() and Phase0Pipeline.run() are synchronous and are dispatched
  to a bounded ThreadPoolExecutor so the event loop stays responsive.
- All FalkorDB *writes* go through a single shared GraphWriter instance
  protected by a threading.Lock.  This serialises writes at the FalkorDB
  connection level regardless of how many concurrent ingestion threads are
  active.
- FalkorDB *reads* (via FalkorClient in app.state.falkor) use a separate
  connection and are not affected by the write lock.

Concurrency safety
------------------
- The per-repo asyncio write_lock (from update_store.RepoSlot) is held for the
  entire Phase 0 run.  A concurrent update or second ingest for the same
  (graph, repo) pair will see the lock held and be rejected with 409 before
  it can start.
- Different repos (each in its own named graph) run in independent slots and
  can execute Phase 0 concurrently.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Worker-package path patching — must happen before importing scanner / phase0
# ---------------------------------------------------------------------------

_WORKER_SRC = str(
    Path(__file__).resolve().parent.parent / "ingestion-worker" / "src"
)
if _WORKER_SRC not in sys.path:
    sys.path.insert(0, _WORKER_SRC)

from graph_writer import GraphWriter  # noqa: E402
from phase0 import Phase0Pipeline     # noqa: E402
from phase1 import Phase1Pipeline     # noqa: E402
from scanner import Scanner, ScanResult  # noqa: E402

from app import job_store, update_store
from app.config import FALKOR_HOST, FALKOR_PASSWORD, FALKOR_PORT

logger = logging.getLogger(__name__)
_SERVICE = "ingestion_service"

# ---------------------------------------------------------------------------
# Shared write connection
# One GraphWriter instance + one threading.Lock for all Phase 0 write threads.
# ---------------------------------------------------------------------------

_write_lock: threading.Lock = threading.Lock()
_shared_writer: Optional[GraphWriter] = None
_writer_init_lock: threading.Lock = threading.Lock()


def _get_shared_writer() -> GraphWriter:
    global _shared_writer
    with _writer_init_lock:
        if _shared_writer is None:
            _shared_writer = GraphWriter(
                host=FALKOR_HOST, port=FALKOR_PORT, password=FALKOR_PASSWORD
            )
            logger.info(
                "Shared write connection initialised",
                extra={"service": _SERVICE, "host": FALKOR_HOST, "port": FALKOR_PORT},
            )
    return _shared_writer


# Bounded executor: limits simultaneous filesystem-scan threads.
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ingest-phase0")


# ---------------------------------------------------------------------------
# Synchronous Phase 0 worker (runs inside executor thread)
# ---------------------------------------------------------------------------


def _run_phase0_sync(graph: str, repo: str, local_path: str) -> ScanResult:
    """Scan the filesystem then batch-write structural nodes to FalkorDB.

    Scanning is I/O-bound but touches no DB — it runs without the write lock.
    All FalkorDB writes are serialised through _write_lock. The ScanResult is
    returned so Phase 1 can plan SCIP indexers from the same source-file list.
    """
    logger.info(
        "Phase 0 scan starting",
        extra={"service": _SERVICE, "graph": graph, "repo": repo, "local_path": local_path},
    )

    scanner = Scanner()
    try:
        scan_result = scanner.scan(graph, repo, local_path)
    except Exception as err:
        logger.error(
            "Scanner failed",
            extra={"service": _SERVICE, "graph": graph, "repo": repo, "error": str(err)},
        )
        raise

    writer = _get_shared_writer()
    with _write_lock:
        logger.info(
            "Phase 0 write lock acquired",
            extra={
                "service": _SERVICE,
                "graph": graph,
                "repo": repo,
                "folders": len(scan_result.folders),
                "files": len(scan_result.files),
            },
        )
        try:
            Phase0Pipeline().run(scan_result, writer)
        except Exception as err:
            logger.error(
                "Phase 0 pipeline write failed",
                extra={"service": _SERVICE, "graph": graph, "repo": repo, "error": str(err)},
            )
            raise

    return scan_result


def _run_phase1_sync(scan_result: ScanResult) -> None:
    """Dispatch SCIP indexers, parse index.scip, and batch-write code symbols.

    Indexer subprocesses and protobuf decoding run without the DB write lock;
    only the final batch write acquires _write_lock (passed into the pipeline),
    so other repositories' Phase 0/1 writes are not blocked during SCIP decode.
    """
    graph = scan_result.graph_name
    repo = scan_result.repo_name

    logger.info(
        "Phase 1 starting",
        extra={
            "service": _SERVICE,
            "graph": graph,
            "repo": repo,
            "source_files": len(scan_result.source_files),
        },
    )

    writer = _get_shared_writer()
    try:
        result = Phase1Pipeline().run(scan_result, writer, write_lock=_write_lock)
    except Exception as err:
        logger.error(
            "Phase 1 pipeline failed",
            extra={"service": _SERVICE, "graph": graph, "repo": repo, "error": str(err)},
        )
        raise

    logger.info(
        "Phase 1 finished",
        extra={
            "service": _SERVICE,
            "graph": graph,
            "repo": repo,
            "nodes_written": result.nodes_written,
            "edges_written": result.edges_written,
            "failed_runs": len(result.failed_runs),
            "parse_errors": result.parse_errors,
        },
    )


# ---------------------------------------------------------------------------
# Public async entry point
# ---------------------------------------------------------------------------


async def run_ingest(
    graph: str,
    repo: str,
    local_path: str,
    job_id: str,
) -> None:
    """Acquire the per-repo asyncio write_lock and execute Phase 0 in a thread pool.

    Holds update_store.RepoSlot.write_lock for the entire duration so that
    concurrent update/recreate requests see a conflict and return 409.
    Updates job_store at each phase transition.
    """
    slot = await update_store.get_or_create_slot(graph, repo)

    logger.info(
        "Ingestion job dispatched",
        extra={"service": _SERVICE, "graph": graph, "repo": repo, "job_id": job_id},
    )

    loop = asyncio.get_running_loop()

    async with slot.write_lock:
        # Phase 0 — filesystem skeleton + structural write.
        await job_store.update_job(graph, repo, "phase0_running", phase="phase0")
        try:
            scan_result = await loop.run_in_executor(
                _EXECUTOR,
                _run_phase0_sync,
                graph,
                repo,
                local_path,
            )
        except Exception as err:
            logger.error(
                "Ingestion failed in Phase 0",
                extra={
                    "service": _SERVICE,
                    "graph": graph,
                    "repo": repo,
                    "job_id": job_id,
                    "error": str(err),
                },
            )
            await job_store.update_job(graph, repo, "failed", error=str(err))
            return

        # Phase 1 — SCIP extraction + code-symbol write.
        await job_store.update_job(graph, repo, "phase1_running", phase="phase1")
        try:
            await loop.run_in_executor(
                _EXECUTOR,
                _run_phase1_sync,
                scan_result,
            )
        except Exception as err:
            logger.error(
                "Ingestion failed in Phase 1",
                extra={
                    "service": _SERVICE,
                    "graph": graph,
                    "repo": repo,
                    "job_id": job_id,
                    "error": str(err),
                },
            )
            await job_store.update_job(graph, repo, "failed", error=str(err))
            return

    await job_store.update_job(graph, repo, "done", phase="done")
    logger.info(
        "Ingestion complete",
        extra={"service": _SERVICE, "graph": graph, "repo": repo, "job_id": job_id},
    )
