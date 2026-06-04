"""Phase 0 pipeline — structural batch-write from a completed ScanResult.

Shared block: used by IngestionService (create path) and, in future, by the
UpdateService recreate path.  Has no knowledge of locks, threads, or job state.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from graph_writer import GraphWriter
    from scanner import ScanResult

logger = logging.getLogger(__name__)
_SERVICE = "phase0"


class Phase0Pipeline:
    """Flush a ScanResult to FalkorDB as structural nodes and CONTAINS edges.

    Call order:
        1. create_indexes
        2. upsert_root  (CodeRepository → Root edge)
        3. batch_upsert_nodes for all Folder nodes
        4. batch_upsert_nodes for all File nodes  (with extra_labels + content_hash)
        5. batch_create_contains_edges for every parent → child pair
        6. mark_repository_ingested
    """

    def run(self, scan_result: "ScanResult", writer: "GraphWriter") -> None:
        gn = scan_result.graph_name
        rn = scan_result.repo_name

        logger.info(
            "Phase 0 write started",
            extra={
                "service": _SERVICE,
                "graph": gn,
                "repo": rn,
                "folders": len(scan_result.folders),
                "files": len(scan_result.files),
                "source_files": len(scan_result.source_files),
            },
        )

        writer.create_indexes(gn)

        writer.upsert_root(gn, rn, "/", scan_result.local_path)
        logger.debug(
            "Root node upserted",
            extra={"service": _SERVICE, "graph": gn, "repo": rn, "root_id": scan_result.root_id},
        )

        if scan_result.folders:
            folder_rows = [
                {
                    "id": f.id,
                    "props": {
                        "path": f.path,
                        "absolute_path": f.absolute_path,
                        "repo_name": f.repo_name,
                        "graph_name": f.graph_name,
                        "order": f.order,
                    },
                    "extra_labels": [],
                }
                for f in scan_result.folders
            ]
            writer.batch_upsert_nodes(gn, folder_rows, "Folder")
            logger.info(
                "Folders written",
                extra={"service": _SERVICE, "graph": gn, "repo": rn, "count": len(folder_rows)},
            )

        if scan_result.files:
            file_rows = [
                {
                    "id": f.id,
                    "props": {
                        "path": f.path,
                        "absolute_path": f.absolute_path,
                        "repo_name": f.repo_name,
                        "graph_name": f.graph_name,
                        "extension": f.extension,
                        "language_guess": f.language_guess,
                        "content_hash": f.content_hash,
                        "order": f.order,
                    },
                    "extra_labels": f.extra_labels,
                }
                for f in scan_result.files
            ]
            writer.batch_upsert_nodes(gn, file_rows, "File")
            logger.info(
                "Files written",
                extra={
                    "service": _SERVICE,
                    "graph": gn,
                    "repo": rn,
                    "count": len(file_rows),
                    "source_count": len(scan_result.source_files),
                },
            )

        # CONTAINS edges: every folder and file points back to its parent.
        all_nodes = [*scan_result.folders, *scan_result.files]
        edges = [{"from": n.parent_id, "to": n.id} for n in all_nodes]
        if edges:
            writer.batch_create_contains_edges(gn, edges)
            logger.info(
                "CONTAINS edges written",
                extra={"service": _SERVICE, "graph": gn, "repo": rn, "count": len(edges)},
            )

        timestamp = datetime.now(timezone.utc).isoformat()
        writer.mark_repository_ingested(gn, rn, timestamp)

        logger.info(
            "Phase 0 complete",
            extra={
                "service": _SERVICE,
                "graph": gn,
                "repo": rn,
                "folder_count": len(scan_result.folders),
                "file_count": len(scan_result.files),
                "source_file_count": len(scan_result.source_files),
                "ingested_at": timestamp,
            },
        )
