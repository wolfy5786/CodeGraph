# CodeGraph — Repository Structure (desktop)

> Annotated monorepo tree for **local-first** packaging: FalkorDB in Docker, single-user FastAPI daemon, CLI, MCP shim, VS Code extension, ingestion worker using **SCIP + regex** (no LSP) with a three-phase crawl.

---

## Design principles

- **Monorepo** — CLI, MCP, web dashboard, ingestion worker, API, Falkor tooling live together.
- **Graph isolation** — one Falkor **named graph per repository** (1:1 with `Graph`/`CodeRepository`); `repo_name` retained for provenance only.
- **Multi-surface UX** — every human/agent action funnels through the same local REST server ([DESKTOP_ARCHITECTURE.md](DESKTOP_ARCHITECTURE.md)).
- **Optional Docker** — Falkor runs in a container; Python services may execute on host (`pipx install codegraph`) or inside compose bundles.

---

## Directory tree

```
codegraph/
├── .env.example                    # Falkor URI, binaries, indexing toggles
├── .gitignore
├── README.md
├── Backend_API.md
├── DESKTOP_ARCHITECTURE.md
├── PHASE1_IMPLEMENTATION.md
├── PHASE2_IMPLEMENTATION.md
├── UPDATE_DESIGN.md                # Update queue, locking, long-poll, and recreate design
├── DEPLOYMENT_CHECKLIST.md         # Desktop install checklist
├── repository_structure.md         # This file
├── docker-compose.yml              # Falkor (+ optional bundled services once implemented)
├── falkor/
│   └── README.md                   # Operational notes for Falkor indexes / GRAPH commands
├── apps/
│   ├── cli/                        # Typer/Click CLI: graph | repo | ingest | serve | mcp shim
│   │   └── src/codegraph_cli/...
│   ├── web/                        # Local dashboard: graphs, ingestion status
│   │   └── src/...
│   ├── vscode-extension/           # TypeScript VS Code UX (REST client)
│   └── mcp-server/                 # stdio MCP -> forwards to local REST
├── services/
│   ├── api/                        # FastAPI — routers/graphs.py, repositories.py
│   │   └── app/routers/...
│   ├── ingestion-worker/           # SCIP crawler + regex labelling + watchers (no LSP)
│   │   └── src/
│   │       ├── worker.py           # dequeue / orchestrate Phase 0 + Phase 1 + Phase 2
│   │       ├── scanner.py          # repo walker + tertiary classification
│   │       ├── scip/
│   │       │   ├── runner.py       # spawn scip-* CLIs per language
│   │       │   └── parser.py       # protobuf -> nodes / occurrences / relationships
│   │       ├── watcher/            # file watcher / incremental job hooks (behavior TBD)
│   │       ├── crawl/              # phase1.py, phase2_base.py, phase2_rules.py, strategies/
│   │       ├── graph_writer.py     # Falkor client wrapper (GRAPH.QUERY)
│   │       └── embeddings/         # optional post-Phase2 builder (strategy TBD)
│   └── retrieval/                  # future NL query stack (architecture TBD)
├── packages/
│   └── shared-types/               # optional generated schemas / constants
├── core_system/                    # Retrieval + label documentation
├── infrastructure/scip/            # vendored/binary instructions for SCIP tooling
├── scripts/                        # maintenance helpers (doctor, dump-graph)
├── tests/
```

---

## Layer mapping

| Layer | Path | Purpose |
|-------|------|---------|
| **CLI** | `apps/cli` | Local operator UX |
| **Dashboard** | `apps/web` | Browser UI on localhost |
| **VS Code extension** | `apps/vscode-extension` | Editor-native flows |
| **MCP shim** | `apps/mcp-server` | MCP tools for other editors/agents |
| **API daemon** | `services/api` | REST entrypoint (`codegraph serve`) |
| **Indexer** | `services/ingestion-worker` | SCIP + regex ingestion (no LSP) |
| **FalkorDB ops** | `falkor/README.md` | Connection + indexing guidance |
| **Core schemas** | `core_system/documentation` | Nodes / relationships authoritative text |

---

## Indexing flow (three-phase recap)

See [PHASE0_IMPLEMENTATION.md](PHASE0_IMPLEMENTATION.md), [PHASE1_IMPLEMENTATION.md](PHASE1_IMPLEMENTATION.md) and [PHASE2_IMPLEMENTATION.md](PHASE2_IMPLEMENTATION.md). High level:

```
Local path (one repo == one named graph)
 -> filesystem skeleton (:Graph/:CodeRepository/:Root/:Folder/:File)
 -> SCIP per SourceFile (+ tertiary whole-file vertices)
 -> Phase 2: SCIP labels -> regex labels/properties -> SCIP relationships (no LSP, no tiers)
 -> optional embeddings (TBD)
```

---

## Key paths cheat sheet

| Concern | Location |
|---------|----------|
| Labels & semantics | `core_system/documentation/Nodes.txt`, `Relationships.txt` |
| External API heuristic JSON | `core_system/config/external_apis/` |
| SCIP tooling | `services/ingestion-worker/src/scip/` |
| Phase 2 strategies | `services/ingestion-worker/src/crawl/strategies/` |
| Falkor playbook | `falkor/README.md` |
| API contract | `Backend_API.md` |
| Operational topology | `DESKTOP_ARCHITECTURE.md` |
