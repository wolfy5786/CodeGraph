# Phase 1 Implementation — Structural skeleton, SCIP nodes, CONTAINS

> **Status:** Desktop redesign (documentation)  
> **Scope:** Structural graph + SCIP-derived code symbols for all indexed languages  
> **Storage:** FalkorDB (one instance, **named graph per workspace** `Graph`)

---

## Design principles

1. **Phase 1 creates all semantic code entities** consumed by Phase 2. No new semantic code nodes appear after Phase 1 (tertiary whole-file nodes are also created here).
2. **Filesystem first** — ingest begins from an **absolute `local_path`** on the developer machine (only local directories are registered).
3. **Hierarchy** — `:Graph → :CodeRepository → :Root → (:Folder|:File) → SCIP hierarchy`. One `CONTAINS` relation type carries ordering metadata at each level.
4. **SCIP for symbol discovery** — for each analyzable `:File`, run the language’s SCIP indexer (or shared multi-lang driver) scoped to that repository root and parse **`index.scip`** into nodes & `CONTAINS` edges rooted at the `:File`.
5. **Extension-based classification** — non-source files collapse to whole-file tertiary nodes (still no embeddings in Phase 1).
6. **Single batch graph write** — accumulate nodes/edges then flush via FalkorDB `GRAPH.<name>` Cypher APIs (implementation detail).

---

## Structural nodes (always created during Phase 1)

| Label | Meaning | Typical properties |
|-------|---------|--------------------|
| `Graph` | Workspace (mirrors Falkor named graph key) | `name`, optional bookkeeping timestamps |
| `CodeRepository` | Registered repo pointing at disk root | `name`, `local_path`, `last_ingested_at`, `graph_name` |
| `Root` | Canonical repo root folder | `path` relative to repo, `absolute_path`, `repo_name`, `graph_name` |
| `Folder` | Directory beneath root | same |
| `File` | On-disk source/config/doc entry | `path`, `repo_name`, `graph_name`, `language_guess`, `extension` |

**Edges**

- `(Graph)-[:CONTAINS {order}]->(CodeRepository)`
- `(CodeRepository)-[:CONTAINS {order:1}]->(Root)`
- `(Folder|Root)-[:CONTAINS]->(Folder|File)`

---

## File classification (by extension — unchanged categories)

Before SCIP invocation:

| File-type label | Extensions / patterns | Processing |
|-----------------|-----------------------|------------|
| `SourceFile` (+ `File`) | `.java`, `.py`, `.kt`, `.scala`, `.go`, `.rs`, `.cpp`, `.c`, `.h`, `.js`, `.ts`, `.tsx`, `.cs`, … | SCIP + future LSP hooks |
| `Dockerfile` | `Dockerfile`, `Dockerfile.*`, `*.dockerfile` | single node |
| `MarkupFile` | `.json`, `.yaml`, `.yml`, `.xml`, `.toml`, `.ini`, `.cfg`, `.properties`, `.html`, … | single node |
| `Documentation` | `.md`, `.txt`, `.rst`, `.adoc` | single node |
| `SQLNoSQLScript` | `.sql`, `.cql`, `.cypher`, `.mongo`, `.hql`, … | single node |
| `CICD` | `.github/workflows/*.yml`, `Jenkinsfile`, `.gitlab-ci.yml`, `.circleci/*`, … | single node |

Tertiary buckets remain mutually exclusive with `SourceFile`.

---

## SCIP extraction (`SourceFile` path)

1. Spawn / reuse per-language toolchain (`scip-java`, `scip-python`, etc.) rooted at **`CodeRepository.local_path`**.
2. Consume emitted `index.scip` protobuf:
   - Map SCIP **`Symbol`** records to deterministic node IDs:
     ```
     id = "{graph_name}:{repo_name}:{relative_path}:{start_line}:{symbol_simple_name}"
     ```
   - Use SCIP **`Document.relative_path`** to bind nodes to `:File`.
   - Recreate hierarchical `CONTAINS` ordering using SCIP **`Symbol.relationship`** + document structure definitions.
3. **File → top-level symbol edge** requirement:
   - **Python / JS / TS**: first SCIP occurrence with `SymbolKind.Package`/`Module`/namespace ⇒ `:Module`-like node anchored under `:File`.
   - **Java / Kotlin**: anchored top-level `:Class`, `:Enum`, `:Interface`.
   - If SCIP emits multiple disjoint top-level siblings, `:File` `CONTAINS` each ordered sibling.

### Stored Phase 1 code-node properties

| Property | Source | Notes |
|----------|--------|-------|
| `id` | Deterministic composite | Matches formula above |
| `name` | SCIP descriptor | Stripped Scheme-like suffix for readability optional |
| `language` | Toolchain config | Mirrors SCIP-language string |
| `path` | Relative to repo root | Mirrors filesystem path |
| `start_line`, `end_line` | SCIP ranges | UTF-16 offsets converted to UTF-8 lines offline |
| `scip_descriptor` | Full SCIP id | Debugging + dedupe |
| `scip_kind` | SCIP `SymbolKind` integer | Consumed directly by Tier 1 |
| `signature`/`detail` | Best-effort from SCIP hover text | Fallback empty until enrichment |

Phase 1 code nodes omit remote object-storage keys and multi-tenant ID fields; they use **`scip_kind`** (SCIP) instead of standalone LSP `SymbolKind` integers from an earlier pipeline sketch.

Whole-file tertiary nodes follow the extension table above without extra persistence keys beyond path metadata.

---

## FalkorDB schema considerations (Phase 1)

FalkorDB does not provide full database-enforced uniqueness constraints for arbitrary properties. **Constraint behavior is application-owned:**

- **Unique `id`** — application-level uniqueness per graph.
- **Secondary indexes**
  - `CREATE INDEX ON :File(path)` — delete-by-relative-path sweep
  - `CREATE INDEX ON :CodeRepository(name)`
  - label indexes where supported for `:Class`, `:Method`, … (implementation-specific)

Consult [falkor/README.md](falkor/README.md) for Redis CLI examples.

Isolation: **queries always specify `GRAPH_NAME`/`GRAPH.QUERY {name}`** — never traverse across workspaces.

---

## Multi-threading

- Parallelize **SCIP emits per-language component** safely (one process invocation per repo *per language toolchain* typically enough).
- **Batch merge** structural nodes + outputs before Falkor flush to reduce round trips.

```
Main:
  Resolve Graph + Repo objects
  Build filesystem Folder/File skeleton (multi-thread walker)
Partition SourceFiles by dominant language SCIP planner
Workers:
    Run SCIP indexer + protobuf decode -> nodes & CONTAINS lists
Merge:
    Append tertiary single-file nodes (no SCIP)
    Single transactional batch write via Falkor client
```

---

## Ingestion flow (desktop API → worker)

1. `POST /graphs/{graph}/repositories` registers `{name, local_path}`.
2. Worker validates path exists/is readable (symlink policies TBD).
3. Acquire graph-scoped ingestion lock (`graph:repo`).
4. **Phase 1** steps listed above execute.
5. Emit structured progress events (`structural-scan`, `scip-complete`, `graph-write`, …) over SSE/WebSocket hooking into dashboard.
6. Store `last_ingested_at` repo property.
7. Enqueue/hand off **Phase 2** job (still same service; doc in `PHASE2_IMPLEMENTATION.md`).

---

## References

- Phase 2: `PHASE2_IMPLEMENTATION.md`
- Structural details: `core_system/documentation/Nodes.txt`
- Relationships: `core_system/documentation/Relationships.txt`
- Desktop layering: `DESKTOP_ARCHITECTURE.md`
- Falkor operations: `falkor/README.md`
