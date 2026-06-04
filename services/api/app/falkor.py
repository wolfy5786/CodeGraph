"""FalkorDB client for API-layer graph and repository operations."""

from __future__ import annotations

import logging
from typing import Any

from falkordb import FalkorDB

from app.config import FALKOR_HOST, FALKOR_PASSWORD, FALKOR_PORT

logger = logging.getLogger(__name__)
_SERVICE = "api.falkor"


class FalkorClient:
    """Thin wrapper around FalkorDB exposing only what the REST API needs."""

    def __init__(self) -> None:
        self._db = FalkorDB(host=FALKOR_HOST, port=FALKOR_PORT, password=FALKOR_PASSWORD)
        logger.info(
            "FalkorClient connected",
            extra={"service": _SERVICE, "host": FALKOR_HOST, "port": FALKOR_PORT},
        )

    # ------------------------------------------------------------------
    # Connectivity
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        try:
            self._db.connection.ping()
            return True
        except Exception as err:
            logger.error(
                "FalkorDB ping failed",
                extra={"service": _SERVICE, "error": str(err)},
            )
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run(self, graph_name: str, query: str, params: dict[str, Any] | None = None) -> Any:
        try:
            return self._db.select_graph(graph_name).query(query, params or {})
        except Exception as err:
            logger.error(
                "FalkorDB query failed",
                extra={
                    "service": _SERVICE,
                    "graph": graph_name,
                    "query_preview": query[:120],
                    "error": str(err),
                },
            )
            raise

    def _ensure_indexes(self, graph_name: str) -> None:
        ddls = [
            "CREATE INDEX FOR (n:CodeRepository) ON (n.name)",
            "CREATE INDEX FOR (n:File) ON (n.path)",
        ]
        for ddl in ddls:
            try:
                self._run(graph_name, ddl)
            except Exception:
                # FalkorDB raises on duplicate CREATE INDEX — swallow silently.
                logger.debug(
                    "Index skipped (likely exists)",
                    extra={"service": _SERVICE, "graph": graph_name},
                )

    # ------------------------------------------------------------------
    # Graph workspace operations
    # ------------------------------------------------------------------

    def list_graphs(self) -> list[str]:
        try:
            return self._db.list_graphs()
        except Exception as err:
            logger.error(
                "Failed to list graphs",
                extra={"service": _SERVICE, "error": str(err)},
            )
            raise

    def graph_exists(self, name: str) -> bool:
        try:
            return name in self._db.list_graphs()
        except Exception as err:
            logger.error(
                "graph_exists check failed",
                extra={"service": _SERVICE, "graph": name, "error": str(err)},
            )
            return False

    def create_graph(self, name: str) -> None:
        self._run(name, "MERGE (g:Graph {name: $name})", {"name": name})
        self._ensure_indexes(name)
        logger.info("Graph created", extra={"service": _SERVICE, "graph": name})

    def delete_graph(self, name: str) -> None:
        try:
            self._db.select_graph(name).delete()
            logger.info("Graph deleted", extra={"service": _SERVICE, "graph": name})
        except Exception as err:
            logger.error(
                "Failed to delete graph",
                extra={"service": _SERVICE, "graph": name, "error": str(err)},
            )
            raise

    def get_graph_stats(self, name: str) -> dict[str, int]:
        repo_res = self._run(
            name,
            "MATCH (r:CodeRepository {graph_name: $gn}) RETURN count(r) AS c",
            {"gn": name},
        )
        repo_count: int = repo_res.result_set[0][0] if repo_res.result_set else 0

        node_res = self._run(name, "MATCH (n) RETURN count(n) AS c")
        node_count: int = node_res.result_set[0][0] if node_res.result_set else 0

        edge_res = self._run(name, "MATCH ()-[r]->() RETURN count(r) AS c")
        edge_count: int = edge_res.result_set[0][0] if edge_res.result_set else 0

        return {"repo_count": repo_count, "node_count": node_count, "edge_count": edge_count}

    # ------------------------------------------------------------------
    # Repository operations
    # ------------------------------------------------------------------

    def repo_exists(self, graph_name: str, repo_name: str) -> bool:
        try:
            res = self._run(
                graph_name,
                "MATCH (r:CodeRepository {name: $rn, graph_name: $gn}) RETURN r LIMIT 1",
                {"rn": repo_name, "gn": graph_name},
            )
            return bool(res.result_set)
        except Exception as err:
            logger.error(
                "repo_exists check failed",
                extra={"service": _SERVICE, "graph": graph_name, "repo": repo_name, "error": str(err)},
            )
            return False

    def attach_repo(self, graph_name: str, repo_name: str, local_path: str) -> None:
        q = (
            "MATCH (g:Graph {name: $gn}) "
            "MERGE (r:CodeRepository {name: $rn, graph_name: $gn}) "
            "SET r.local_path = $lp "
            "MERGE (g)-[:CONTAINS {order: 1}]->(r)"
        )
        self._run(graph_name, q, {"gn": graph_name, "rn": repo_name, "lp": local_path})
        logger.info(
            "Repository attached",
            extra={"service": _SERVICE, "graph": graph_name, "repo": repo_name},
        )

    def list_repos(self, graph_name: str) -> list[dict[str, Any]]:
        res = self._run(
            graph_name,
            "MATCH (r:CodeRepository {graph_name: $gn}) "
            "RETURN r.name, r.local_path, r.last_ingested_at",
            {"gn": graph_name},
        )
        return [
            {
                "name": row[0],
                "local_path": row[1],
                "last_ingested_at": row[2],
            }
            for row in res.result_set
        ]

    def get_repo_local_path(self, graph_name: str, repo_name: str) -> str | None:
        """Return local_path for the registered repository, or None if not found."""
        try:
            res = self._run(
                graph_name,
                "MATCH (r:CodeRepository {name: $rn, graph_name: $gn}) RETURN r.local_path",
                {"rn": repo_name, "gn": graph_name},
            )
            rows = res.result_set
            return rows[0][0] if rows else None
        except Exception as err:
            logger.error(
                "get_repo_local_path failed",
                extra={
                    "service": _SERVICE,
                    "graph": graph_name,
                    "repo": repo_name,
                    "error": str(err),
                },
            )
            return None

    def delete_repo(self, graph_name: str, repo_name: str) -> None:
        self._run(graph_name, "MATCH (n {repo_name: $rn}) DETACH DELETE n", {"rn": repo_name})
        self._run(
            graph_name,
            "MATCH (r:CodeRepository {name: $rn, graph_name: $gn}) DETACH DELETE r",
            {"rn": repo_name, "gn": graph_name},
        )
        logger.info(
            "Repository deleted",
            extra={"service": _SERVICE, "graph": graph_name, "repo": repo_name},
        )
