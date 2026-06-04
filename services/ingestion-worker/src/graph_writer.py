"""FalkorDB CRUD client for Phase 1 structural and Phase 2 semantic ingestion."""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from typing import Any

from falkordb import FalkorDB

logger = logging.getLogger(__name__)

_HOST = os.getenv("FALKOR_HOST", "127.0.0.1")
_PORT = int(os.getenv("FALKOR_PORT", "6379"))
_PASSWORD = os.getenv("FALKOR_PASSWORD")


def _node_to_dict(node: Any) -> dict[str, Any]:
    return {"_labels": list(node.labels), **node.properties}


class GraphWriter:
    """FalkorDB wrapper exposing Phase 1 and Phase 2 CRUD operations."""

    def __init__(
        self,
        host: str = _HOST,
        port: int = _PORT,
        password: str | None = _PASSWORD,
    ) -> None:
        self._client = FalkorDB(host=host, port=port, password=password)
        logger.info(
            "GraphWriter connected",
            extra={"service": "graph_writer", "host": host, "port": port},
        )

    def _graph(self, graph_name: str) -> Any:
        return self._client.select_graph(graph_name)

    def _run(
        self,
        graph_name: str,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        try:
            return self._graph(graph_name).query(query, params or {})
        except Exception as err:
            logger.error(
                "FalkorDB query failed",
                extra={
                    "service": "graph_writer",
                    "graph": graph_name,
                    "query_preview": query[:120],
                    "error": str(err),
                },
            )
            raise

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def create_indexes(self, graph_name: str) -> None:
        """Idempotently create property indexes for common traversal patterns."""
        ddls = [
            "CREATE INDEX FOR (n:File) ON (n.path)",
            "CREATE INDEX FOR (n:CodeRepository) ON (n.name)",
            "CREATE INDEX FOR (n:CodeRepository) ON (n.graph_name)",
            "CREATE INDEX FOR (n:Folder) ON (n.path)",
            "CREATE INDEX FOR (n:Class) ON (n.name)",
            "CREATE INDEX FOR (n:Method) ON (n.name)",
            "CREATE INDEX FOR (n:Function) ON (n.name)",
        ]
        for ddl in ddls:
            try:
                self._run(graph_name, ddl)
            except Exception:
                # FalkorDB raises on duplicate CREATE INDEX — swallow and continue.
                logger.debug(
                    "Index skipped (likely exists)",
                    extra={"service": "graph_writer", "graph": graph_name, "ddl": ddl},
                )
        logger.info("Indexes ensured", extra={"service": "graph_writer", "graph": graph_name})

    # ------------------------------------------------------------------
    # Phase 1 — structural writes
    # ------------------------------------------------------------------

    def upsert_graph(self, graph_name: str) -> None:
        """Merge the :Graph workspace anchor node."""
        self._run(graph_name, "MERGE (g:Graph {name: $name})", {"name": graph_name})
        logger.debug("Upserted Graph", extra={"service": "graph_writer", "graph": graph_name})

    def upsert_repository(
        self,
        graph_name: str,
        repo_name: str,
        local_path: str,
    ) -> None:
        """Merge :CodeRepository and attach to :Graph via CONTAINS."""
        q = (
            "MATCH (g:Graph {name: $gn}) "
            "MERGE (r:CodeRepository {name: $rn, graph_name: $gn}) "
            "SET r.local_path = $lp "
            "MERGE (g)-[:CONTAINS {order: 1}]->(r)"
        )
        self._run(graph_name, q, {"gn": graph_name, "rn": repo_name, "lp": local_path})
        logger.info(
            "Upserted CodeRepository",
            extra={"service": "graph_writer", "graph": graph_name, "repo": repo_name},
        )

    def mark_repository_ingested(
        self, graph_name: str, repo_name: str, timestamp: str
    ) -> None:
        """Set last_ingested_at on :CodeRepository after a successful Phase 1 run."""
        q = (
            "MATCH (r:CodeRepository {name: $rn, graph_name: $gn}) "
            "SET r.last_ingested_at = $ts"
        )
        self._run(graph_name, q, {"gn": graph_name, "rn": repo_name, "ts": timestamp})
        logger.info(
            "Marked ingested",
            extra={"service": "graph_writer", "graph": graph_name, "repo": repo_name},
        )

    def upsert_root(
        self,
        graph_name: str,
        repo_name: str,
        path: str,
        absolute_path: str,
    ) -> str:
        """Merge :Root and CONTAINS from :CodeRepository. Returns node id."""
        node_id = f"{graph_name}:{repo_name}:/"
        q = (
            "MATCH (r:CodeRepository {name: $rn, graph_name: $gn}) "
            "MERGE (root:Root {id: $id}) "
            "SET root.path = $path, root.absolute_path = $ap, "
            "    root.repo_name = $rn, root.graph_name = $gn "
            "MERGE (r)-[:CONTAINS {order: 1}]->(root)"
        )
        self._run(
            graph_name, q,
            {"gn": graph_name, "rn": repo_name, "id": node_id, "path": path, "ap": absolute_path},
        )
        return node_id

    def upsert_folder(
        self,
        graph_name: str,
        repo_name: str,
        path: str,
        parent_id: str,
        order: int = 0,
    ) -> str:
        """Merge :Folder and CONTAINS from parent (Root or Folder). Returns node id."""
        node_id = f"{graph_name}:{repo_name}:{path}"
        q = (
            "MATCH (p {id: $parent_id}) "
            "MERGE (f:Folder {id: $id}) "
            "SET f.path = $path, f.repo_name = $rn, f.graph_name = $gn "
            "MERGE (p)-[:CONTAINS {order: $order}]->(f)"
        )
        self._run(
            graph_name, q,
            {"parent_id": parent_id, "id": node_id, "path": path,
             "rn": repo_name, "gn": graph_name, "order": order},
        )
        return node_id

    def upsert_file(
        self,
        graph_name: str,
        repo_name: str,
        path: str,
        extension: str,
        language_guess: str,
        parent_id: str,
        extra_labels: list[str],
        order: int = 0,
    ) -> str:
        """Merge :File with overlay labels and CONTAINS from parent. Returns node id."""
        node_id = f"{graph_name}:{repo_name}:{path}"
        q = (
            "MATCH (p {id: $parent_id}) "
            "MERGE (f:File {id: $id}) "
            "SET f.path = $path, f.repo_name = $rn, f.graph_name = $gn, "
            "    f.extension = $ext, f.language_guess = $lang "
            "MERGE (p)-[:CONTAINS {order: $order}]->(f)"
        )
        self._run(
            graph_name, q,
            {"parent_id": parent_id, "id": node_id, "path": path,
             "rn": repo_name, "gn": graph_name, "ext": extension,
             "lang": language_guess, "order": order},
        )
        if extra_labels:
            self.batch_add_labels(graph_name, [(node_id, lbl) for lbl in extra_labels])
        return node_id

    def upsert_code_node(
        self,
        graph_name: str,
        props: dict[str, Any],
        labels: list[str],
    ) -> None:
        """Merge a SCIP-derived code symbol; first label is the MERGE anchor."""
        if not labels:
            raise ValueError("code node requires at least one label")
        primary = labels[0]
        q = f"MERGE (n:{primary} {{id: $id}}) SET n += $props"
        self._run(graph_name, q, {"id": props["id"], "props": props})
        if len(labels) > 1:
            self.batch_add_labels(graph_name, [(props["id"], lbl) for lbl in labels[1:]])

    def create_contains_edge(
        self,
        graph_name: str,
        from_id: str,
        to_id: str,
        order: int | None = None,
    ) -> None:
        """MERGE a single CONTAINS edge between two nodes by id."""
        edge_props = f" {{order: {order}}}" if order is not None else ""
        q = (
            f"MATCH (a {{id: $fid}}), (b {{id: $tid}}) "
            f"MERGE (a)-[:CONTAINS{edge_props}]->(b)"
        )
        self._run(graph_name, q, {"fid": from_id, "tid": to_id})

    def batch_upsert_nodes(
        self,
        graph_name: str,
        nodes: list[dict[str, Any]],
        primary_label: str,
    ) -> None:
        """Bulk MERGE nodes sharing a primary label. Each item needs id, props, extra_labels."""
        if not nodes:
            return
        q = (
            f"UNWIND $rows AS row "
            f"MERGE (n:{primary_label} {{id: row.id}}) "
            f"SET n += row.props"
        )
        self._run(graph_name, q, {"rows": [{"id": n["id"], "props": n["props"]} for n in nodes]})
        pairs = [(n["id"], lbl) for n in nodes for lbl in n.get("extra_labels", [])]
        if pairs:
            self.batch_add_labels(graph_name, pairs)
        logger.debug(
            "Batch upserted nodes",
            extra={
                "service": "graph_writer",
                "graph": graph_name,
                "label": primary_label,
                "count": len(nodes),
            },
        )

    def batch_create_contains_edges(
        self,
        graph_name: str,
        edges: list[dict[str, Any]],
    ) -> None:
        """Bulk MERGE CONTAINS edges. Each item: {"from": str, "to": str}."""
        if not edges:
            return
        q = (
            "UNWIND $edges AS e "
            "MATCH (a {id: e.from_id}), (b {id: e.to_id}) "
            "MERGE (a)-[:CONTAINS]->(b)"
        )
        self._run(graph_name, q, {"edges": [{"from_id": e["from"], "to_id": e["to"]} for e in edges]})
        logger.debug(
            "Batch created CONTAINS edges",
            extra={"service": "graph_writer", "graph": graph_name, "count": len(edges)},
        )

    def delete_file_subtree(
        self, graph_name: str, repo_name: str, path: str
    ) -> None:
        """Delete a File and all SCIP symbol descendants for incremental re-ingestion."""
        params = {"rn": repo_name, "path": path}
        # Descendants first so DETACH DELETE on the file node finds no dangling edges.
        self._run(
            graph_name,
            "MATCH (:File {repo_name: $rn, path: $path})-[:CONTAINS*]->(desc) DETACH DELETE desc",
            params,
        )
        self._run(
            graph_name,
            "MATCH (f:File {repo_name: $rn, path: $path}) DETACH DELETE f",
            params,
        )
        logger.info(
            "Deleted file subtree",
            extra={"service": "graph_writer", "graph": graph_name, "repo": repo_name, "path": path},
        )

    def delete_repository(self, graph_name: str, repo_name: str) -> None:
        """Detach-delete all nodes scoped to repo_name including the CodeRepository anchor."""
        self._run(graph_name, "MATCH (n {repo_name: $rn}) DETACH DELETE n", {"rn": repo_name})
        self._run(
            graph_name,
            "MATCH (r:CodeRepository {name: $rn, graph_name: $gn}) DETACH DELETE r",
            {"rn": repo_name, "gn": graph_name},
        )
        logger.info(
            "Deleted repository",
            extra={"service": "graph_writer", "graph": graph_name, "repo": repo_name},
        )

    # ------------------------------------------------------------------
    # Phase 2 — read queries
    # ------------------------------------------------------------------

    def get_nodes_by_label(
        self, graph_name: str, repo_name: str, label: str
    ) -> list[dict[str, Any]]:
        """All nodes with label scoped to repo_name."""
        q = f"MATCH (n:{label} {{repo_name: $rn}}) RETURN n"
        result = self._run(graph_name, q, {"rn": repo_name})
        return [_node_to_dict(row[0]) for row in result.result_set]

    def get_node_by_id(
        self, graph_name: str, nid: str
    ) -> dict[str, Any] | None:
        """Fetch a single node by application-level id."""
        result = self._run(graph_name, "MATCH (n {id: $nid}) RETURN n", {"nid": nid})
        rows = result.result_set
        return _node_to_dict(rows[0][0]) if rows else None

    def get_contains_parent(
        self, graph_name: str, nid: str
    ) -> dict[str, Any] | None:
        """Return the immediate CONTAINS parent of nid."""
        result = self._run(
            graph_name,
            "MATCH (p)-[:CONTAINS]->(n {id: $nid}) RETURN p",
            {"nid": nid},
        )
        rows = result.result_set
        return _node_to_dict(rows[0][0]) if rows else None

    def get_contains_children(
        self, graph_name: str, nid: str
    ) -> list[dict[str, Any]]:
        """Direct CONTAINS children ordered by start_line."""
        q = "MATCH (n {id: $nid})-[:CONTAINS]->(c) RETURN c ORDER BY c.start_line"
        result = self._run(graph_name, q, {"nid": nid})
        return [_node_to_dict(row[0]) for row in result.result_set]

    def get_nodes_by_name_and_label(
        self,
        graph_name: str,
        repo_name: str,
        name: str,
        label: str,
    ) -> list[dict[str, Any]]:
        """Resolve a simple type name to nodes — used for BELONGS_TO and INHERITS resolution."""
        q = f"MATCH (n:{label} {{repo_name: $rn, name: $name}}) RETURN n"
        result = self._run(graph_name, q, {"rn": repo_name, "name": name})
        return [_node_to_dict(row[0]) for row in result.result_set]

    def get_inherits_parents(
        self, graph_name: str, class_id: str
    ) -> list[dict[str, Any]]:
        """Return nodes that class_id directly INHERITS from."""
        result = self._run(
            graph_name,
            "MATCH (c {id: $cid})-[:INHERITS]->(p) RETURN p",
            {"cid": class_id},
        )
        return [_node_to_dict(row[0]) for row in result.result_set]

    def get_methods_of_class(
        self, graph_name: str, class_id: str
    ) -> list[dict[str, Any]]:
        """Return :Method nodes directly contained by class_id."""
        result = self._run(
            graph_name,
            "MATCH (c {id: $cid})-[:CONTAINS]->(m:Method) RETURN m",
            {"cid": class_id},
        )
        return [_node_to_dict(row[0]) for row in result.result_set]

    def get_class_hierarchy(
        self, graph_name: str, repo_name: str
    ) -> list[tuple[str, str, str]]:
        """Return (child_id, parent_id, parent_name) for all INHERITS chains in repo."""
        q = (
            "MATCH (c:Class {repo_name: $rn})-[:INHERITS*]->(p) "
            "RETURN c.id, p.id, p.name"
        )
        result = self._run(graph_name, q, {"rn": repo_name})
        return [(row[0], row[1], row[2]) for row in result.result_set]

    def get_nodes_by_scip_kind_and_parent_kind(
        self,
        graph_name: str,
        repo_name: str,
        child_scip_kind: int,
        parent_scip_kind: int,
    ) -> list[dict[str, Any]]:
        """Child nodes filtered by SCIP kind with parent id attached as _parent_id."""
        q = (
            "MATCH (p {scip_kind: $pk})-[:CONTAINS]->(n {repo_name: $rn, scip_kind: $k}) "
            "RETURN n, p.id AS parent_id"
        )
        result = self._run(
            graph_name, q, {"rn": repo_name, "k": child_scip_kind, "pk": parent_scip_kind}
        )
        out = []
        for row in result.result_set:
            d = _node_to_dict(row[0])
            d["_parent_id"] = row[1]
            out.append(d)
        return out

    # ------------------------------------------------------------------
    # Phase 2 — write methods (also exposed for WAL rollback)
    # ------------------------------------------------------------------

    def add_label(self, graph_name: str, nid: str, label: str) -> None:
        """Add a label to an existing node."""
        self._run(graph_name, f"MATCH (n {{id: $nid}}) SET n:{label}", {"nid": nid})

    def remove_label(self, graph_name: str, nid: str, label: str) -> None:
        """Remove a label from an existing node."""
        self._run(graph_name, f"MATCH (n {{id: $nid}}) REMOVE n:{label}", {"nid": nid})

    def set_property(
        self, graph_name: str, nid: str, key: str, value: Any
    ) -> None:
        """Set one property on a node — used by WAL rollback to restore old values."""
        self._run(
            graph_name,
            "MATCH (n {id: $nid}) SET n += $props",
            {"nid": nid, "props": {key: value}},
        )

    def batch_add_labels(
        self, graph_name: str, pairs: list[tuple[str, str]]
    ) -> None:
        """Add labels to multiple nodes, grouped by label to minimise query round-trips."""
        # Cypher cannot parameterise label names, so one query per unique label is required.
        by_label: dict[str, list[str]] = defaultdict(list)
        for nid, label in pairs:
            by_label[label].append(nid)
        for label, ids in by_label.items():
            self._run(
                graph_name,
                f"UNWIND $ids AS nid MATCH (n {{id: nid}}) SET n:{label}",
                {"ids": ids},
            )

    def batch_set_properties(
        self, graph_name: str, updates: list[dict[str, Any]]
    ) -> None:
        """Bulk property merge. Each item: {"id": str, "props": dict}."""
        if not updates:
            return
        self._run(
            graph_name,
            "UNWIND $updates AS u MATCH (n {id: u.id}) SET n += u.props",
            {"updates": updates},
        )

    def batch_create_edges(
        self,
        graph_name: str,
        edges: list[dict[str, str]],
        rel_type: str,
    ) -> None:
        """Bulk MERGE typed edges. Each item: {"from": str, "to": str}."""
        if not edges:
            return
        q = (
            f"UNWIND $edges AS e "
            f"MATCH (a {{id: e.from_id}}), (b {{id: e.to_id}}) "
            f"MERGE (a)-[:{rel_type}]->(b)"
        )
        self._run(graph_name, q, {"edges": [{"from_id": e["from"], "to_id": e["to"]} for e in edges]})
        logger.debug(
            "Batch created edges",
            extra={
                "service": "graph_writer",
                "graph": graph_name,
                "rel_type": rel_type,
                "count": len(edges),
            },
        )

    def delete_edge(
        self, graph_name: str, from_id: str, to_id: str, rel_type: str
    ) -> None:
        """Delete a single typed edge — called by WAL rollback."""
        q = f"MATCH (a {{id: $fid}})-[r:{rel_type}]->(b {{id: $tid}}) DELETE r"
        self._run(graph_name, q, {"fid": from_id, "tid": to_id})
