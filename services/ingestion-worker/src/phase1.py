"""Phase 1 pipeline — SCIP extraction and code-symbol graph write.

Receives a ScanResult from Phase 0, partitions source files by language,
spawns the appropriate SCIP indexer per language concurrently, decodes each
emitted ``index.scip`` into code-symbol nodes and ``CONTAINS`` edges, and
batch-writes them to FalkorDB (separate from the Phase 0 structural write).

See ``documentation/PHASE1_IMPLEMENTATION.md``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from scip_parser import CodeSymbolNode, ContainsEdge, parse_scip_file

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from graph_writer import GraphWriter
    from scanner import ScanResult

logger = logging.getLogger(__name__)
_SERVICE = "phase1"

_DEFAULT_TIMEOUT_S: int = int(os.getenv("SCIP_INDEXER_TIMEOUT_S", "600"))
_MAX_PARALLEL_INDEXERS: int = int(os.getenv("SCIP_MAX_PARALLEL_INDEXERS", "4"))


# ---------------------------------------------------------------------------
# Indexer configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScipIndexerConfig:
    """CLI template for a single-language SCIP indexer.

    base_cmd: tokens forming the command; {repo_name} is substituted at runtime.
    output_flag: flag the tool accepts for the output path (e.g. "--output").
                 Set to None for tools that always write index.scip to their CWD;
                 those tools are run with cwd=output_dir instead.
    cwd_to_output: when True the process CWD is set to output_dir rather than
                   repo_root (for tools that ignore --output).
    output_filename: name the tool actually writes inside output_dir.
    """

    language: str
    base_cmd: tuple[str, ...]
    output_flag: str | None = "--output"
    cwd_to_output: bool = False
    output_filename: str = "index.scip"

    def build_cmd(self, repo_name: str, output_path: str) -> list[str]:
        cmd = [t.replace("{repo_name}", repo_name) for t in self.base_cmd]
        if self.output_flag:
            cmd += [self.output_flag, output_path]
        return cmd


# Maps language_guess (from scanner) → indexer config.
# javascript shares the scip-typescript toolchain.
# c/cpp share scip-clang; kotlin gets its own binary.
_SCIP_CONFIGS: dict[str, ScipIndexerConfig] = {
    "python": ScipIndexerConfig(
        language="python",
        base_cmd=("scip-python", "index", ".", "--project-name", "{repo_name}"),
        output_flag="--output",
    ),
    "typescript": ScipIndexerConfig(
        language="typescript",
        base_cmd=("scip-typescript", "index"),
        output_flag="--output",
    ),
    "javascript": ScipIndexerConfig(
        language="javascript",
        # scip-typescript handles .js/.mjs as well
        base_cmd=("scip-typescript", "index"),
        output_flag="--output",
    ),
    "java": ScipIndexerConfig(
        language="java",
        base_cmd=("scip-java", "index"),
        output_flag="--output",
    ),
    "kotlin": ScipIndexerConfig(
        language="kotlin",
        base_cmd=("scip-kotlin",),
        output_flag="--output",
    ),
    "scala": ScipIndexerConfig(
        language="scala",
        base_cmd=("scip-scala",),
        output_flag="--output",
    ),
    "go": ScipIndexerConfig(
        language="go",
        base_cmd=("scip-go",),
        output_flag="--output",
    ),
    "rust": ScipIndexerConfig(
        language="rust",
        # rust-analyzer scip writes index.scip to CWD; no --output flag accepted
        base_cmd=("rust-analyzer", "scip", "."),
        output_flag=None,
        cwd_to_output=True,
    ),
    "cpp": ScipIndexerConfig(
        language="cpp",
        base_cmd=("scip-clang", "index"),
        output_flag="--output",
    ),
    "c": ScipIndexerConfig(
        language="c",
        # scip-clang handles C as well
        base_cmd=("scip-clang", "index"),
        output_flag="--output",
    ),
    "csharp": ScipIndexerConfig(
        language="csharp",
        base_cmd=("dotnet-scip",),
        output_flag="--output",
    ),
    "ruby": ScipIndexerConfig(
        language="ruby",
        base_cmd=("scip-ruby",),
        output_flag="--output",
    ),
    "php": ScipIndexerConfig(
        language="php",
        base_cmd=("scip-php",),
        output_flag="--output",
    ),
    "swift": ScipIndexerConfig(
        language="swift",
        base_cmd=("scip-swift",),
        output_flag="--output",
    ),
}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class IndexerRun:
    """Outcome of a single language's SCIP indexer subprocess."""

    language: str
    files_count: int        # number of SourceFile nodes for this language
    cmd: list[str]          # full command that was (or would have been) run
    repo_root: str
    output_dir: str
    scip_path: str | None   # absolute path to index.scip; None if the run failed
    exit_code: int | None   # None when the process timed out or could not start
    stderr: str
    duration_ms: float

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and self.scip_path is not None


@dataclass
class Phase1Result:
    """Aggregated outcome of all indexer runs and the code-symbol write."""

    graph_name: str
    repo_name: str
    runs: list[IndexerRun] = field(default_factory=list)
    nodes_written: int = 0
    edges_written: int = 0
    parse_errors: int = 0

    @property
    def successful_runs(self) -> list[IndexerRun]:
        return [r for r in self.runs if r.succeeded]

    @property
    def failed_runs(self) -> list[IndexerRun]:
        return [r for r in self.runs if not r.succeeded]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STDERR_HEAD_CHARS: int = int(os.getenv("SCIP_STDERR_HEAD_CHARS", "1500"))
_STDERR_TAIL_CHARS: int = int(os.getenv("SCIP_STDERR_TAIL_CHARS", "500"))


def _split_head_tail(text: str) -> tuple[str, str]:
    """Return (head, tail) slices of indexer stderr for logging.

    The head holds the thrown error type and the offending file (top of a
    Node/Pyright stack trace); the tail holds the bottom frames. When the text
    fits inside the head budget the tail is returned empty to avoid duplication.
    """
    head = text[:_STDERR_HEAD_CHARS]
    tail = text[-_STDERR_TAIL_CHARS:] if len(text) > _STDERR_HEAD_CHARS else ""
    return head, tail


def _output_dir(graph_name: str, repo_name: str, language: str) -> Path:
    """Deterministic scratch directory for one indexer run.

    Uses the system temp dir so output survives the process without
    polluting the repository working tree.
    """
    return (
        Path(tempfile.gettempdir())
        / "codegraph_scip"
        / graph_name
        / repo_name
        / language
    )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class Phase1Pipeline:
    """SCIP extraction → code-symbol nodes/edges → batch graph write.

    Call order expected by the ingestion service:
        1. Phase0Pipeline.run(scan_result, writer)
        2. Phase1Pipeline.run(scan_result, writer)   ← this class

    ``run`` dispatches the SCIP indexers, decodes each ``index.scip`` into
    code-symbol nodes and ``CONTAINS`` edges, and (when a writer is supplied)
    flushes them to FalkorDB in a single batch.
    """

    def __init__(
        self,
        timeout_s: int = _DEFAULT_TIMEOUT_S,
        max_workers: int = _MAX_PARALLEL_INDEXERS,
    ) -> None:
        self._timeout_s = timeout_s
        self._max_workers = max_workers

    def run(
        self,
        scan_result: "ScanResult",
        writer: "GraphWriter | None" = None,
        write_lock: "AbstractContextManager | None" = None,
    ) -> Phase1Result:
        gn = scan_result.graph_name
        rn = scan_result.repo_name

        source_files = scan_result.source_files
        if not source_files:
            logger.info(
                "Phase 1 skipped — no source files in scan result",
                extra={"service": _SERVICE, "graph": gn, "repo": rn},
            )
            return Phase1Result(graph_name=gn, repo_name=rn)

        # Count source files per language for planning and log context.
        by_language: dict[str, int] = {}
        for f in source_files:
            by_language[f.language_guess] = by_language.get(f.language_guess, 0) + 1

        logger.info(
            "Phase 1 started",
            extra={
                "service": _SERVICE,
                "graph": gn,
                "repo": rn,
                "total_source_files": len(source_files),
                "languages": by_language,
            },
        )

        # Partition into planned (have a SCIP config) vs skipped (no config).
        planned: list[tuple[str, int, ScipIndexerConfig]] = []
        skipped: list[str] = []
        for lang in sorted(by_language):
            config = _SCIP_CONFIGS.get(lang)
            if config is None:
                skipped.append(lang)
            else:
                planned.append((lang, by_language[lang], config))

        if skipped:
            logger.warning(
                "Languages have no SCIP indexer configured — skipping",
                extra={
                    "service": _SERVICE,
                    "graph": gn,
                    "repo": rn,
                    "skipped_languages": skipped,
                },
            )

        if not planned:
            logger.warning(
                "Phase 1 complete — no indexers could be dispatched",
                extra={"service": _SERVICE, "graph": gn, "repo": rn},
            )
            return Phase1Result(graph_name=gn, repo_name=rn)

        logger.info(
            "Dispatching SCIP indexers",
            extra={
                "service": _SERVICE,
                "graph": gn,
                "repo": rn,
                "indexers": [lang for lang, _, _ in planned],
                "concurrency": min(self._max_workers, len(planned)),
            },
        )

        runs: list[IndexerRun] = []
        with ThreadPoolExecutor(max_workers=min(self._max_workers, len(planned))) as pool:
            futures = {
                pool.submit(self._run_indexer, lang, count, config, scan_result): lang
                for lang, count, config in planned
            }
            for future in as_completed(futures):
                lang = futures[future]
                try:
                    runs.append(future.result())
                except Exception as exc:
                    logger.error(
                        "Indexer worker thread raised unexpectedly",
                        extra={
                            "service": _SERVICE,
                            "graph": gn,
                            "repo": rn,
                            "language": lang,
                            "error": str(exc),
                        },
                    )
                    runs.append(IndexerRun(
                        language=lang,
                        files_count=by_language[lang],
                        cmd=[],
                        repo_root=scan_result.local_path,
                        output_dir="",
                        scip_path=None,
                        exit_code=None,
                        stderr=str(exc),
                        duration_ms=0.0,
                    ))

        successful = [r for r in runs if r.succeeded]
        failed = [r for r in runs if not r.succeeded]

        logger.info(
            "SCIP indexers complete",
            extra={
                "service": _SERVICE,
                "graph": gn,
                "repo": rn,
                "total_runs": len(runs),
                "successful": len(successful),
                "failed": len(failed),
                "scip_paths": [r.scip_path for r in successful],
            },
        )

        result = Phase1Result(graph_name=gn, repo_name=rn, runs=runs)
        self._parse_and_write(result, writer, write_lock)
        return result

    # ------------------------------------------------------------------
    # SCIP parse + batch write
    # ------------------------------------------------------------------

    def _parse_and_write(
        self,
        result: Phase1Result,
        writer: "GraphWriter | None",
        write_lock: "AbstractContextManager | None" = None,
    ) -> None:
        """Decode every successful index.scip into nodes/edges and flush once.

        Parsing runs without ``write_lock``; only the batch write acquires it so
        concurrent ingests for other repos are not blocked during SCIP decode.
        """
        gn = result.graph_name
        rn = result.repo_name

        nodes: dict[str, CodeSymbolNode] = {}
        edges: dict[tuple[str, str], ContainsEdge] = {}
        for run in result.successful_runs:
            try:
                graph = parse_scip_file(run.scip_path, gn, run.language)
            except Exception as err:  # noqa: BLE001 — one bad index must not abort
                result.parse_errors += 1
                logger.error(
                    "Failed to parse SCIP index",
                    extra={
                        "service": _SERVICE,
                        "graph": gn,
                        "repo": rn,
                        "language": run.language,
                        "scip_path": run.scip_path,
                        "error": str(err),
                    },
                )
                continue
            result.parse_errors += graph.parse_errors
            for node in graph.nodes:
                nodes.setdefault(node.id, node)          # first writer wins on id clash
            for edge in graph.edges:
                edges.setdefault((edge.from_id, edge.to_id), edge)

        if not nodes:
            logger.info(
                "Phase 1 complete — no code symbols extracted",
                extra={
                    "service": _SERVICE,
                    "graph": gn,
                    "repo": rn,
                    "parse_errors": result.parse_errors,
                },
            )
            return

        if writer is None:
            logger.warning(
                "Phase 1 parsed code symbols but no writer was supplied — skipping write",
                extra={"service": _SERVICE, "graph": gn, "repo": rn, "nodes": len(nodes)},
            )
            return

        if write_lock is not None:
            with write_lock:
                self._write_graph(writer, gn, rn, list(nodes.values()), list(edges.values()))
        else:
            self._write_graph(writer, gn, rn, list(nodes.values()), list(edges.values()))
        result.nodes_written = len(nodes)
        result.edges_written = len(edges)

        logger.info(
            "Phase 1 complete",
            extra={
                "service": _SERVICE,
                "graph": gn,
                "repo": rn,
                "nodes_written": result.nodes_written,
                "edges_written": result.edges_written,
                "parse_errors": result.parse_errors,
            },
        )

    def _write_graph(
        self,
        writer: "GraphWriter",
        graph_name: str,
        repo_name: str,
        nodes: list[CodeSymbolNode],
        edges: list[ContainsEdge],
    ) -> None:
        """Batch-write code-symbol nodes (grouped by label) then CONTAINS edges."""
        by_label: dict[str, list[dict]] = defaultdict(list)
        for node in nodes:
            by_label[node.primary_label].append({
                "id": node.id,
                "props": node.to_props(graph_name, repo_name),
                "extra_labels": ["CodeVertex"],
            })

        for label, rows in by_label.items():
            writer.batch_upsert_nodes(graph_name, rows, label)
            logger.info(
                "Code-symbol nodes written",
                extra={
                    "service": _SERVICE,
                    "graph": graph_name,
                    "repo": repo_name,
                    "label": label,
                    "count": len(rows),
                },
            )

        edge_rows = [
            {"from": e.from_id, "to": e.to_id, "order": e.order} for e in edges
        ]
        writer.batch_create_contains_edges_ordered(graph_name, edge_rows)
        logger.info(
            "Code CONTAINS edges written",
            extra={
                "service": _SERVICE,
                "graph": graph_name,
                "repo": repo_name,
                "count": len(edge_rows),
            },
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_indexer(
        self,
        language: str,
        files_count: int,
        config: ScipIndexerConfig,
        scan_result: "ScanResult",
    ) -> IndexerRun:
        gn = scan_result.graph_name
        rn = scan_result.repo_name
        repo_root = scan_result.local_path

        output_dir = _output_dir(gn, rn, language)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(output_dir / config.output_filename)

        cmd = config.build_cmd(repo_name=rn, output_path=output_path)
        cwd = str(output_dir) if config.cwd_to_output else repo_root

        logger.info(
            "Spawning SCIP indexer",
            extra={
                "service": _SERVICE,
                "graph": gn,
                "repo": rn,
                "language": language,
                "files_count": files_count,
                "cmd": cmd,
                "cwd": cwd,
                "output_path": output_path,
                "timeout_s": self._timeout_s,
            },
        )

        start = time.monotonic()
        exit_code: int | None = None
        stderr_text = ""

        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
            )
            exit_code = proc.returncode
            stderr_text = proc.stderr or ""

            if exit_code != 0:
                stderr_head, stderr_tail = _split_head_tail(stderr_text)
                logger.error(
                    "SCIP indexer exited with non-zero code",
                    extra={
                        "service": _SERVICE,
                        "graph": gn,
                        "repo": rn,
                        "language": language,
                        "exit_code": exit_code,
                        # Head carries the thrown error/type and offending file;
                        # tail carries the bottom of the stack trace.
                        "stderr_head": stderr_head,
                        "stderr_tail": stderr_tail,
                        "stderr_len": len(stderr_text),
                    },
                )
            else:
                logger.debug(
                    "SCIP indexer stdout",
                    extra={
                        "service": _SERVICE,
                        "graph": gn,
                        "repo": rn,
                        "language": language,
                        "stdout_tail": (proc.stdout or "")[-500:],
                    },
                )

        except FileNotFoundError:
            stderr_text = f"binary not found on PATH: {cmd[0]}"
            logger.error(
                "SCIP indexer binary not found on PATH",
                extra={
                    "service": _SERVICE,
                    "graph": gn,
                    "repo": rn,
                    "language": language,
                    "binary": cmd[0],
                },
            )
        except subprocess.TimeoutExpired:
            stderr_text = f"timed out after {self._timeout_s}s"
            logger.error(
                "SCIP indexer timed out",
                extra={
                    "service": _SERVICE,
                    "graph": gn,
                    "repo": rn,
                    "language": language,
                    "timeout_s": self._timeout_s,
                },
            )

        duration_ms = round((time.monotonic() - start) * 1000, 1)

        # Verify the output file was actually produced before calling it a success.
        scip_path: str | None = None
        if exit_code == 0:
            if Path(output_path).is_file():
                scip_path = output_path
                logger.info(
                    "SCIP index produced",
                    extra={
                        "service": _SERVICE,
                        "graph": gn,
                        "repo": rn,
                        "language": language,
                        "scip_path": scip_path,
                        "duration_ms": duration_ms,
                    },
                )
            else:
                logger.warning(
                    "SCIP indexer exited 0 but output file not found",
                    extra={
                        "service": _SERVICE,
                        "graph": gn,
                        "repo": rn,
                        "language": language,
                        "expected_path": output_path,
                        "duration_ms": duration_ms,
                    },
                )

        return IndexerRun(
            language=language,
            files_count=files_count,
            cmd=cmd,
            repo_root=repo_root,
            output_dir=str(output_dir),
            scip_path=scip_path,
            exit_code=exit_code,
            stderr=stderr_text,
            duration_ms=duration_ms,
        )
