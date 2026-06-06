# Phase 0 Implementation — Filesystem Scan, Classification, Content Hashing, Structural Graph Write

> **Status:** Desktop redesign (documentation)
> **Scope:** Walk the repository filesystem, classify every entry by extension, hash file content, write the complete structural skeleton (Folder/File nodes + CONTAINS edges) to FalkorDB in a single batch.
> **Storage:** FalkorDB (one instance, **named graph per workspace** `Graph`)

---

## Purpose

Phase 0 is the first step of every ingestion run. It produces the structural skeleton of the repository graph and records a content hash on every file node. Downstream phases (Phase 1 SCIP, Phase 2 tier DAG) consume the Phase 0 output but do not create new structural nodes.

---

## Design principles

1. **No external tool invocation** — Phase 0 is pure filesystem I/O. No SCIP, no LSP, no embeddings.
2. **Single batch write** — all FolderNode and FileNode records (with edges) are accumulated in memory and flushed to FalkorDB in one batch at the end of the phase.
3. **Classification stored on node** — the file-type label and `language_guess` are determined here via extension matching and written as node properties. Phase 1 reads these labels to decide which files to send to SCIP.
4. **Content hash stored on node** — each FileNode carries a SHA-256 hex digest of the file's raw bytes at scan time. The update pipeline compares this hash against the current on-disk hash to decide whether a file has been modified since the last ingestion.
5. **Depth-first, alphabetical ordering** — `os.walk(topdown=True)` with sorted `dirnames` produces a pre-order depth-first traversal. The `order` property on each node encodes alphabetical sibling position and is written to the `CONTAINS` edge.

---

## Structural nodes created in Phase 0

| Label | Meaning | Key properties |
|-------|---------|----------------|
| `Graph` | Workspace (mirrors FalkorDB named graph key) | `name` |
| `CodeRepository` | Registered repo pointing at disk root | `name`, `local_path`, `graph_name` |
| `Root` | Canonical repo root folder | `name` (repo root dir basename), `path` (posix, relative), `absolute_path`, `repo_name`, `graph_name` |
| `Folder` | Directory beneath root | `id`, `name` (folder basename), `path`, `absolute_path`, `repo_name`, `graph_name`, `order` |
| `File` | On-disk file entry | `id`, `name` (filename with extension), `path`, `absolute_path`, `repo_name`, `graph_name`, `extension`, `language_guess`, `extra_labels`, `content_hash`, `order` |

> `name` is the display caption used in graph visualisations — it is the basename of the node's path (e.g. `"utils"` for a folder, `"main.py"` for a file, `"my-repo"` for the root directory).

> `Graph` and `CodeRepository` nodes are created during workspace / repo registration, not during Phase 0 itself. Phase 0 creates `Root`, `Folder`, and `File` nodes.

### Edges created in Phase 0

| Relationship | From → To | Properties |
|--------------|-----------|------------|
| `CONTAINS` | `CodeRepository → Root` | `order: 1` |
| `CONTAINS` | `Root → Folder\|File` | `order` (alphabetical sibling position) |
| `CONTAINS` | `Folder → Folder\|File` | `order` |

---

## File classification (extension-based)

Each file is classified exactly once during the scan. The resulting label(s) are stored in the `extra_labels` list property on the FileNode and written to FalkorDB.

| Extra label | Extensions / patterns | Phase 1 treatment |
|-------------|-----------------------|-------------------|
| `SourceFile` | `.java`, `.py`, `.kt`, `.scala`, `.go`, `.rs`, `.cpp`, `.cxx`, `.cc`, `.hpp`, `.c`, `.h`, `.js`, `.mjs`, `.ts`, `.tsx`, `.cs`, `.rb`, `.php`, `.swift` | Sent to SCIP indexer |
| `Dockerfile` | `Dockerfile`, `Dockerfile.*`, `*.dockerfile` | Whole-file node; no SCIP |
| `MarkupFile` | `.json`, `.yaml`, `.yml`, `.xml`, `.toml`, `.ini`, `.cfg`, `.properties`, `.html`, `.htm`, `.xhtml` | Whole-file node; no SCIP |
| `Documentation` | `.md`, `.txt`, `.rst`, `.adoc` | Whole-file node; no SCIP |
| `SQLNoSQLScript` | `.sql`, `.cql`, `.cypher`, `.mongo`, `.hql` | Whole-file node; no SCIP |
| `CICD` | `.github/workflows/*.yml`, `Jenkinsfile`, `.gitlab-ci.yml`, `.circleci/*` | Whole-file node; no SCIP |
| *(none)* | Unrecognised extension | Whole-file node; no SCIP |

Categories are mutually exclusive. `CICD` and `Dockerfile` are checked before extension matching so a `.yml` under `.github/workflows/` is never classified as `MarkupFile`.

`language_guess` is set to the language string (e.g. `"python"`, `"java"`) for `SourceFile` entries and to an empty string for all other categories.

---

## Content hashing

After classifying a file, Phase 0 reads its raw bytes and computes a **SHA-256 hex digest**, stored as `content_hash` on the `File` node.

**Purpose:** The update pipeline (`check_update`) compares the on-disk SHA-256 of each tracked file against the stored `content_hash` to detect modifications without relying on filesystem modification timestamps (which can be unreliable after clones or syncs).

**Edge cases:**

| Situation | Behaviour |
|-----------|-----------|
| File is unreadable (permissions, broken symlink) | Log a `warn`; set `content_hash` to `""` and continue |
| Binary file | Hash raw bytes as-is; no text decoding attempted |
| Empty file | Hash of zero bytes; `content_hash` is the standard SHA-256 of empty input |

---

## Phase 0 execution flow

```
1. Resolve root_abs = Path(local_path).resolve()
2. Walk filesystem depth-first (os.walk, topdown=True)
   For each directory:
     a. Prune skipped dirs (_SKIP_DIRS) in-place
     b. Sort remaining entries alphabetically
     c. For each child directory  → create FolderNode, record in dir_id_map
     d. For each child file
          i.  classify_file(rel_posix) → language_guess, extra_labels
          ii. hash_file(absolute_path) → content_hash
          iii. create FileNode
3. Accumulate all FolderNode + FileNode records in memory (ScanResult)
4. Batch-write all nodes + CONTAINS edges to FalkorDB in a single transaction
5. Emit structured log: folder_count, file_count, source_file_count
6. Hand off ScanResult to Phase 1
```

Skipped directory names (never descended into):

```
.git  .svn  .hg  node_modules  __pycache__  .venv  venv  dist  build  target  .gradle  .idea  .vscode
```

---

## Node ID scheme

All node IDs follow a deterministic composite key:

```
id = "{graph_name}:{repo_name}:{rel_posix_path}"
```

The repo root gets the fixed suffix `/`:

```
root_id = "{graph_name}:{repo_name}:/"
```

IDs are stable across re-ingestions for the same `(graph_name, repo_name, path)` triple, which allows the update pipeline to merge rather than re-create nodes.

---

## FalkorDB schema considerations (Phase 0)

- `CREATE INDEX ON :File(path)` — enables the update pipeline to look up nodes by relative path.
- `CREATE INDEX ON :File(content_hash)` — optional; useful for bulk change-detection queries.
- `CREATE INDEX ON :Folder(path)`
- Uniqueness is application-enforced via the deterministic `id` scheme.

---

## Relationship to other phases

| Phase | Reads from Phase 0 | Adds to graph |
|-------|--------------------|---------------|
| Phase 0 | — | Root, Folder, File nodes; CONTAINS edges |
| Phase 1 | `ScanResult.source_files` (FileNodes with `SourceFile` label) | Code symbol nodes (Class, Method, Function, …); CONTAINS edges under each File |
| Phase 2 | Phase 1 code nodes | Semantic edges (CALLS, OVERRIDES, IMPORTS, …); enriched properties |

---

## References

- Scanner implementation: `services/ingestion-worker/src/scanner.py`
- Phase 1 (SCIP): [PHASE1_IMPLEMENTATION.md](PHASE1_IMPLEMENTATION.md)
- Phase 2 (tier DAG): [PHASE2_IMPLEMENTATION.md](PHASE2_IMPLEMENTATION.md)
- Update pipeline (uses `content_hash`): [UPDATE_DESIGN.md](UPDATE_DESIGN.md)
- FalkorDB operations: [falkor/README.md](../falkor/README.md)
- Node label reference: `core_system/documentation/Nodes.txt`
- Relationship reference: `core_system/documentation/Relationships.txt`
