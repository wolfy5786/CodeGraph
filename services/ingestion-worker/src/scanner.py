"""Filesystem scanner — produces Phase 0 structural skeleton for GraphWriter."""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".svn", ".hg",
    "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", "target", ".gradle", ".idea", ".vscode",
})

_SOURCE_EXT_LANGUAGE: dict[str, str] = {
    ".java": "java",    ".py": "python",  ".kt": "kotlin",   ".scala": "scala",
    ".go": "go",        ".rs": "rust",
    ".cpp": "cpp",      ".cxx": "cpp",    ".cc": "cpp",      ".hpp": "cpp",
    ".c": "c",          ".h": "c",
    ".js": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".cs": "csharp",    ".rb": "ruby",    ".php": "php",     ".swift": "swift",
}

_MARKUP_EXTS: frozenset[str] = frozenset({
    ".json", ".yaml", ".yml", ".xml", ".toml", ".ini",
    ".cfg", ".properties", ".html", ".htm", ".xhtml",
})

_DOC_EXTS: frozenset[str] = frozenset({".md", ".txt", ".rst", ".adoc"})

_SQL_EXTS: frozenset[str] = frozenset({".sql", ".cql", ".cypher", ".mongo", ".hql"})

_CICD_EXACT_NAMES: frozenset[str] = frozenset({"Jenkinsfile", ".gitlab-ci.yml"})


def hash_file(abs_path: str) -> str:
    """SHA-256 hex digest of file bytes. Returns empty string if the file is unreadable."""
    try:
        h = hashlib.sha256()
        with open(abs_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError as err:
        logger.warning(
            "Could not hash file; content_hash set to empty string",
            extra={"service": "scanner", "path": abs_path, "error": str(err)},
        )
        return ""


def _is_cicd(rel_posix: str, filename: str) -> bool:
    if filename in _CICD_EXACT_NAMES:
        return True
    parts = PurePosixPath(rel_posix).parts
    if len(parts) >= 3 and parts[0] == ".github" and parts[1] == "workflows":
        return True
    return len(parts) >= 2 and parts[0] == ".circleci"


def _is_dockerfile(filename: str) -> bool:
    lower = filename.lower()
    return lower == "dockerfile" or lower.startswith("dockerfile.") or lower.endswith(".dockerfile")


def classify_file(rel_posix: str) -> tuple[str, list[str]]:
    """Return (language_guess, extra_labels) for a posix-relative file path.

    language_guess is an empty string for non-source files.
    extra_labels are overlay labels added on top of the base :File label.
    Categories are mutually exclusive; CICD and Dockerfile are checked first
    so a .yml in .github/workflows/ is not classified as MarkupFile.
    """
    p = PurePosixPath(rel_posix)
    filename = p.name
    ext = p.suffix.lower()

    if _is_cicd(rel_posix, filename):
        return "", ["CICD"]
    if _is_dockerfile(filename):
        return "", ["Dockerfile"]
    if ext in _SOURCE_EXT_LANGUAGE:
        return _SOURCE_EXT_LANGUAGE[ext], ["SourceFile"]
    if ext in _MARKUP_EXTS:
        return "", ["MarkupFile"]
    if ext in _DOC_EXTS:
        return "", ["Documentation"]
    if ext in _SQL_EXTS:
        return "", ["SQLNoSQLScript"]
    return "", []


@dataclass
class FolderNode:
    id: str
    name: str           # folder basename — used as display caption in graph
    path: str           # posix-style, relative to repo root
    absolute_path: str
    repo_name: str
    graph_name: str
    parent_id: str
    order: int          # position among siblings, alphabetical


@dataclass
class FileNode:
    id: str
    name: str           # filename (with extension) — used as display caption in graph
    path: str           # posix-style, relative to repo root
    absolute_path: str
    repo_name: str
    graph_name: str
    extension: str
    language_guess: str  # empty string for non-source files
    extra_labels: list[str]
    parent_id: str
    order: int          # position among siblings, alphabetical
    content_hash: str   # SHA-256 hex digest of raw file bytes at scan time; "" if unreadable

    @property
    def is_source(self) -> bool:
        return "SourceFile" in self.extra_labels


@dataclass
class ScanResult:
    graph_name: str
    repo_name: str
    local_path: str     # resolved absolute path of repo root
    root_id: str        # "{graph_name}:/"
    folders: list[FolderNode] = field(default_factory=list)
    files: list[FileNode] = field(default_factory=list)

    @property
    def source_files(self) -> list[FileNode]:
        """Files that require SCIP indexing."""
        return [f for f in self.files if f.is_source]


class Scanner:
    """Walk a repository root and return a Phase 1 structural skeleton.

    Produces FolderNode and FileNode records ready to be handed to GraphWriter.
    Does not touch FalkorDB and does not invoke SCIP.
    """

    def __init__(self, skip_dirs: frozenset[str] | None = None) -> None:
        self._skip_dirs = skip_dirs if skip_dirs is not None else _SKIP_DIRS

    def scan(self, graph_name: str, repo_name: str, local_path: str) -> ScanResult:
        root_abs = Path(local_path).resolve()
        if not root_abs.is_dir():
            raise ValueError(f"local_path is not a directory: {local_path}")

        logger.info(
            "Filesystem scan started",
            extra={
                "service": "scanner",
                "graph": graph_name,
                "repo": repo_name,
                "local_path": str(root_abs),
            },
        )

        # One named graph per repository, so the graph name alone scopes node IDs;
        # repo_name is still stored on each node as a provenance property.
        root_id = f"{graph_name}:/"
        folders: list[FolderNode] = []
        files: list[FileNode] = []
        # Populated as we descend so each directory can look up its parent id.
        dir_id_map: dict[Path, str] = {root_abs: root_id}

        for dirpath_str, dirnames, filenames in os.walk(root_abs, topdown=True):
            dirpath = Path(dirpath_str)
            # Prune in-place so os.walk does not descend into skipped dirs.
            dirnames[:] = sorted(d for d in dirnames if d not in self._skip_dirs)
            parent_id = dir_id_map[dirpath]
            self._visit(
                dirpath, dirnames, filenames,
                root_abs, parent_id, graph_name, repo_name,
                dir_id_map, folders, files,
            )

        logger.info(
            "Filesystem scan complete",
            extra={
                "service": "scanner",
                "graph": graph_name,
                "repo": repo_name,
                "folder_count": len(folders),
                "file_count": len(files),
                "source_file_count": sum(1 for f in files if f.is_source),
            },
        )
        return ScanResult(
            graph_name=graph_name,
            repo_name=repo_name,
            local_path=str(root_abs),
            root_id=root_id,
            folders=folders,
            files=files,
        )

    def _visit(
        self,
        dirpath: Path,
        dirnames: list[str],
        filenames: list[str],
        root_abs: Path,
        parent_id: str,
        graph_name: str,
        repo_name: str,
        dir_id_map: dict[Path, str],
        folders: list[FolderNode],
        files: list[FileNode],
    ) -> None:
        # Sort folders and files together so sibling order is consistent.
        all_children = sorted(
            [(d, "dir") for d in dirnames] + [(f, "file") for f in filenames]
        )
        for order, (name, kind) in enumerate(all_children):
            child_abs = dirpath / name
            rel_posix = child_abs.relative_to(root_abs).as_posix()
            node_id = f"{graph_name}:{rel_posix}"

            if kind == "dir":
                dir_id_map[child_abs] = node_id
                folders.append(FolderNode(
                    id=node_id,
                    name=name,
                    path=rel_posix,
                    absolute_path=str(child_abs),
                    repo_name=repo_name,
                    graph_name=graph_name,
                    parent_id=parent_id,
                    order=order,
                ))
            else:
                lang, extra_labels = classify_file(rel_posix)
                files.append(FileNode(
                    id=node_id,
                    name=name,
                    path=rel_posix,
                    absolute_path=str(child_abs),
                    repo_name=repo_name,
                    graph_name=graph_name,
                    extension=PurePosixPath(name).suffix.lower(),
                    language_guess=lang,
                    extra_labels=extra_labels,
                    parent_id=parent_id,
                    order=order,
                    content_hash=hash_file(str(child_abs)),
                ))
