# FalkorDB (CodeGraph workspace graphs)

[FalkorDB](https://www.falkordb.com/) runs as Redis module graph engine. Desktop CodeGraph mounts **exactly one database instance locally** while storing **many logical graphs** (one Falkor named graph per workspace `Graph`).

---

## Prerequisites

- Docker CLI or compatible runtime (`docker compose` optional)
- `redis-cli` (usually bundled with Redis distribution)

---

## Launch (quick)

```bash
docker run --name codegraph-falkor -p 6379:6379 -d falkordb/falkordb
```

Expose only on `127.0.0.1` for single-user ergonomics (`-p 127.0.0.1:6379:6379`) when hardened.

Compose snippet (conceptual):

```yaml
services:
  falkordb:
    image: falkordb/falkordb
    ports:
      - "6379:6379"
```

---

## GRAPH commands

Interactive shell:

```bash
redis-cli -h 127.0.0.1 -p 6379
GRAPH.QUERY <graph_name> "MATCH (n) RETURN count(n) LIMIT 25"
GRAPH.LIST                              # enumerate graphs managed by Falkor module
GRAPH.DELETE <graph_name>                 # wipes entire workspace graph — irreversible
```

> `<graph_name>` equals the canonical workspace slug created via `POST /api/v1/graphs`.

---

## Indexes & uniqueness

Declarative uniqueness constraints analogous to classic graph DB DDL are **limited** here. Enforcement strategy:

1. **`id` property** uniqueness per node — ingestion worker merges/dedupes deliberately.
2. Create secondary indexes aggressively for Falkor-supported patterns (syntax evolves with Falkor releases—verify upstream docs):

   - `CALL db.idx.fulltext.createNodeIndex(...)` equivalents if full-text enabled (optional future)
   - Property indexes commonly needed:
     ```cypher
     CREATE INDEX FOR (f:File) ON (f.path)
     CREATE INDEX FOR (r:CodeRepository) ON (r.name)
     ```

Adapt statements to Falkor-supported grammar (some variants require `GRAPH.QUERY` wrapper only).

Because constraints are soft, ingestion jobs should:

- Upsert deterministic ids (`GRAPH.MERGE` patterns or delete-then-batch-insert).
- Maintain **repo-level** bookkeeping when deleting subgraphs (`repo_name`, path prefix nukes).

---

## Backup / restore tips

Use Redis persistence (`appendonly yes`, snapshots) configured on Falkor volume. For reproducible resets during dev wipe container volume.

---

## Cross-links

- API graph lifecycle: [`Backend_API.md`](../Backend_API.md)
- Operational desktop doc: [`DESKTOP_ARCHITECTURE.md`](../DESKTOP_ARCHITECTURE.md)
- Phase writers: [`PHASE1_IMPLEMENTATION.md`](../PHASE1_IMPLEMENTATION.md)
