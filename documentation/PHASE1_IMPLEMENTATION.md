# Phase 1 Implementation — SCIP symbol extraction and code-node graph write

> **Status:** Desktop redesign (documentation)
> **Scope:** Run SCIP indexers over `SourceFile` entries produced by Phase 0, parse the emitted `index.scip` protobuf into code-symbol nodes and `CONTAINS` edges, and batch-write everything to FalkorDB.
> **Storage:** FalkorDB (one instance, **named graph per workspace** `Graph`)

---

## Design principles

1. **Phase 1 creates all semantic code entities** consumed by Phase 2. No new code-symbol nodes appear after Phase 1.
2. **Structural skeleton is pre-built** — Phase 0 has already written all `Root`, `Folder`, and `File` nodes with `CONTAINS` edges. Phase 1 only appends code-symbol nodes under existing `:File` nodes.
3. **SCIP for symbol discovery** — for each `SourceFile`, run the language's SCIP indexer (or shared multi-lang driver) scoped to the repository root and parse the resulting `index.scip` into nodes and `CONTAINS` edges rooted at each `:File`.
4. **Single batch write** — accumulate all code-symbol nodes and edges, then flush to FalkorDB in one batch (separate from the Phase 0 structural write).

---

## Input

Phase 1 receives a `ScanResult` from Phase 0. The relevant subset is `ScanResult.source_files` — the list of `FileNode` records whose `extra_labels` contains `"SourceFile"`.

Non-source files (Dockerfile, MarkupFile, Documentation, SQLNoSQLScript, CICD, unclassified) are fully represented as whole-file nodes after Phase 0 and require no Phase 1 processing.

---

## SCIP extraction

### Indexer invocation

For each language group present in `source_files`:

1. Spawn the language-specific SCIP indexer (`scip-java`, `scip-python`, `scip-typescript`, `scip-clang`, …) rooted at **`CodeRepository.local_path`**.
2. Indexers for different languages may run concurrently (one process per language toolchain per repo).
3. Each indexer emits `index.scip` (protobuf); parse it immediately after the process exits.

### Symbol-to-node mapping

| SCIP concept | Graph node |
|--------------|------------|
| Top-level `Module` / `Package` (Python, JS, TS) | `:Module` anchored under `:File` |
| Top-level `Class` / `Enum` / `Interface` (Java, Kotlin, …) | `:Class` / `:Enum` / `:Interface` anchored under `:File` |
| Nested `Method`, `Function`, `Field` | Nested under their enclosing top-level node |

If SCIP emits multiple disjoint top-level siblings in a single file, `:File CONTAINS` each ordered sibling.

### Node ID scheme

```
id = "{graph_name}:{repo_name}:{relative_path}:{start_line}:{symbol_simple_name}"
```

IDs are deterministic and stable across re-ingestions for the same source position.

### Stored properties

| Property | Source | Notes |
|----------|--------|-------|
| `id` | Deterministic composite | Matches formula above |
| `name` | SCIP descriptor | Stripped Scheme-like suffix for readability (optional) |
| `language` | Toolchain config | Mirrors SCIP-language string |
| `path` | Relative to repo root | Mirrors parent `:File` path |
| `start_line`, `end_line` | SCIP ranges | UTF-16 offsets converted to UTF-8 lines offline |
| `scip_descriptor` | Full SCIP id | Debugging + dedupe |
| `scip_kind` | SCIP `SymbolKind` integer | Consumed directly by Phase 2 Tier 1 |
| `signature` / `detail` | SCIP hover text (best-effort) | Empty until Phase 2 enrichment |

---

## Edges added in Phase 1

| Relationship | From → To | Properties |
|--------------|-----------|------------|
| `CONTAINS` | `File → top-level symbol` | `order` (position among siblings) |
| `CONTAINS` | `top-level symbol → nested symbol` | `order` |

No structural edges (`Root/Folder CONTAINS File`) are created in Phase 1; those were written by Phase 0.

---

## Multi-threading

```
Main (after Phase 0):
  Partition ScanResult.source_files by dominant language → SCIP planner

Workers (one per language toolchain):
  Run SCIP indexer → decode index.scip protobuf → produce nodes & CONTAINS lists

Merge:
  Collect all worker outputs
  Single transactional batch write to FalkorDB (code-symbol nodes + CONTAINS edges)
```

---

## Ingestion flow (desktop API → worker)

1. `POST /graphs/{graph}/repositories` registers `{name, local_path}`.
2. Worker validates path exists and is readable.
3. Acquire graph-scoped ingestion lock (`graph:repo`).
4. **Phase 0** — filesystem scan, classification, content hashing, structural graph write.
5. **Phase 1** — SCIP extraction (steps above); batch-write code-symbol nodes.
6. Emit structured progress events (`structural-scan`, `scip-complete`, `graph-write`, …) over SSE/WebSocket.
7. Store `last_ingested_at` on the `CodeRepository` node.
8. Hand off to **Phase 2** (documented in `PHASE2_IMPLEMENTATION.md`).

---

## FalkorDB schema considerations (Phase 1)

- `CREATE INDEX ON :Class(name)`, `CREATE INDEX ON :Method(name)`, … — label indexes for symbol lookup.
- Uniqueness is application-enforced via the deterministic `id` scheme (same approach as Phase 0).

Consult [falkor/README.md](../falkor/README.md) for Redis CLI examples and index creation commands.

Isolation: queries always specify the FalkorDB named graph — never traverse across workspaces.

---

## References

- Phase 0 (structural skeleton): [PHASE0_IMPLEMENTATION.md](PHASE0_IMPLEMENTATION.md)
- Phase 2 (tier DAG): [PHASE2_IMPLEMENTATION.md](PHASE2_IMPLEMENTATION.md)
- Structural details: `core_system/documentation/Nodes.txt`
- Relationships: `core_system/documentation/Relationships.txt`
- Desktop layering: [DESKTOP_ARCHITECTURE.md](DESKTOP_ARCHITECTURE.md)
- FalkorDB operations: [falkor/README.md](../falkor/README.md)
