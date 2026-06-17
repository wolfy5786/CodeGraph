# CodeGraph — Backend API (desktop / local)

> REST surface for the FastAPI daemon (`services/api`) bundled with **`codegraph serve`**. Served on **`http://127.0.0.1:8765`** by convention. **No authentication** — binds to localhost; do not expose to untrusted networks.

---

## Overview

| Concern | Details |
|---------|---------|
| **Base URL** | `http://127.0.0.1:8765/api/v1` |
| **Auth** | None (local single-user assumption) |
| **Middleware** | Optional request timing / gzip only |
| **Graph scope** | All writes target a FalkorDB **named graph** matching the `{name}` path segment (one graph per repository) |
| **Repository scope** | Each named graph holds exactly one `{repo}` (1:1 with the graph); no cross-repo filtering needed |
| **Natural-language query** | Reserved route — **design TBD** (see README *Query flow (TBD)*) |

---

## 1. Graph workspace management

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/graphs` | Create named repository graph (`body: {"name":"<graph_key>"}`) |
| `GET` | `/api/v1/graphs` | Enumerate graphs known to daemon |
| `GET` | `/api/v1/graphs/{name}` | Metadata & coarse stats (#repos, disk paths, Falkor cardinality summary) |
| `DELETE` | `/api/v1/graphs/{name}` | Drops FalkorDB graph + clears local bookkeeping |

Responses return JSON; errors use HTTP problem-detail style `{ "detail": "..." }`.

---

## 2. Repository registration & ingestion

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/graphs/{name}/repositories` | Attach repo (`{"name":"<repo>","local_path":"/abs/path"}`) |
| `GET` | `/api/v1/graphs/{name}/repositories` | List repositories & paths |
| `DELETE` | `/api/v1/graphs/{name}/repositories/{repo}` | Remove repo subgraph + bookkeeping |
| `POST` | `/api/v1/graphs/{name}/repositories/{repo}/ingest` | Kick off Phase 1+2 ingestion; SSE by `Accept` or `text/event-stream` |
| `GET` | `/api/v1/graphs/{name}/repositories/{repo}/status` | Current job state machine (`queued`, `phase1`, `phase2`, `embedding?`, `done`, `failed`) |

### Update operations (async queue — see [UPDATE_DESIGN.md](UPDATE_DESIGN.md))

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/graphs/{name}/repositories/{repo}/check-update` | Enqueue auto-detect delta; returns `202 Accepted` |
| `POST` | `/api/v1/graphs/{name}/repositories/{repo}/update` | Enqueue explicit file-delta manifest; returns `202 Accepted` |
| `POST` | `/api/v1/graphs/{name}/repositories/{repo}/recreate` | Enqueue full teardown + re-ingest; returns `202 Accepted` |
| `GET` | `/api/v1/graphs/{name}/repositories/{repo}/updates/poll` | Long-poll: holds until current update cycle completes, then delivers result |

All update requests are **non-blocking** — they enqueue a job and return immediately. Concurrent reads during an update are served from a saved snapshot with `"update_in_progress": true` metadata. See [UPDATE_DESIGN.md](UPDATE_DESIGN.md) for queue combining rules, read-lock semantics, and long-poll behavior.

### Attach repository (`POST .../repositories`)

```json
{
  "name": "payments-service",
  "local_path": "C:\\dev\\payments-service"
}
```

Paths must resolve to readable directories **on this machine**.

### Trigger ingest (`POST .../ingest`)

```json
{
  "local_path": "/abs/path/on/this/machine",
  "options": {}
}
```

> `local_path` is required so jobs can relocate if the symlink moved; must match canonical registration path unless overridden explicitly.

Incremental behavior is **documented separately** (`README.md` Incremental Updates TBD) — endpoints stay stable regardless.

---

## 3. Query (reserved — TBD)

| Method | Endpoint | Notes |
|--------|----------|-------|
| `POST` | `/api/v1/graphs/{name}/query` | **Not finalized.** Body/response schemas intentionally omitted until retrieval redesign lands. Likely eventual fields: `{ "prompt": "", "strategy": "", "repos": [...] }`. |

Temporary clients should **avoid** coupling until changelog announces completion.

---

## 4. Health & diagnostics

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Liveness (process + Falkor ping) |
| `GET` | `/api/v1/version` | Build metadata |

---

## Summary table

| Category | Endpoints |
|----------|-----------|
| **Graphs** | `POST/GET /graphs`, `GET/DELETE /graphs/{name}` |
| **Repositories** | `POST/GET /graphs/{name}/repositories`, `DELETE .../{repo}` |
| **Ingest** | `POST .../{repo}/ingest`, `GET .../{repo}/status` |
| **Query** | `POST .../query` (**TBD**) |
| **System** | `GET /health`, `GET /api/v1/version` |

---

## Isolation guarantees (local desktop)

- **Graph isolation**: one Falkor **named graph per repository** — never traverse across `{name}`.
- **Repository scope**: each graph holds exactly one repo (1:1); `repo_name` is retained on nodes for provenance only.
- **Trusted local deployment**: bind the API to loopback unless you deliberately add your own authentication or tunneling layer.
