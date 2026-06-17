# Phase 2 Implementation -- Semantic Labels and Relationships

> **Status**: Redesigned (documentation)
> **Prerequisite**: Phase 1 must be complete (filesystem skeleton + `:File` linkage + semantic symbols with `scip_kind` / `scip_descriptor` / `detail`, all `CONTAINS` edges written to the FalkorDB **named graph** for this repository).
> **Core rule**: Phase 2 does NOT create new code-symbol nodes. It only adds labels, properties, and relationships to the existing node set created in Phase 1.

---

## Design Principles

1. **No new nodes.** Phase 2 operates exclusively on the node set created by Phase 1. (The only exception is an `External` stub already materialized in Phase 1 for a referenced-but-undefined symbol — Phase 2 labels it, it does not create it.)
2. **Single pass — no tiers, no DAG, no ordering.** Every label, property, and relationship is derived independently from data that already exists when Phase 2 starts:
   - **SCIP** — the SCIP index parsed in Phase 1 (`scip_kind`, the `Occurrence` stream, and `Relationship` records). Source of all *definition* labels, the `Object` label, the `External` label, and the relationships `INHERITS`, `IMPLEMENTS`, `CALLS`, `SETS`, `GETS`, `OVERRIDES`, `BELONGS_TO`.
   - **Regex** — language-specific source patterns. Source of every remaining label (modifier/intent labels) and the scalar properties.
   There is **no LSP** in the ingestion pipeline.
3. **No node or relationship depends on another Phase-2 output.** Each node's labels/properties are a pure function of its own `scip_kind` / `scip_descriptor` / source text. Each relationship is a `(source_symbol, target_symbol)` pair drawn from SCIP (or a regex match), and **both endpoints are resolved to node ids through the symbol→id map built up front** (see *Symbol Resolution*), never by reading a label or edge that Phase 2 itself wrote. Because nothing reads Phase-2 state, the work can run in any order / fully in parallel and still be deterministic.
4. **One graph per repository.** Each repository is its own FalkorDB named graph (`GRAPH.QUERY <repo_graph>`). Phase 2 reads and writes only inside that single graph, so no `repo_name` cross-repo filtering is required.
5. **Transactional memcache.** Every write batch is recorded in a single Phase-2 write-ahead log. On failure, the entire Phase 2 transaction is reversed using compensating queries.
6. **Strategy Pattern with Common Rule Registry.** Base logic lives in one file. Language-specific rules live in per-language strategy files. Common rules handle the SCIP kind-based mapping (~70% of labels); languages extend with regex patterns.

---

## Why a single pass is sufficient

The earlier design split Phase 2 into ordered passes because two relationships resolved their targets by reading the *graph state* Phase 2 was concurrently writing:

- `BELONGS_TO` matched `(:Class {name: …})` — i.e. it depended on the `Class` label having been written first.
- `OVERRIDES` had a graph-traversal fallback that walked `INHERITS`/`IMPLEMENTS` edges — i.e. it depended on those edges existing first.

Both dependencies are removed by resolving relationship targets through the **SCIP symbol → node id** map instead of through Phase-2 labels/edges:

- `BELONGS_TO` resolves the field's declared-type **SCIP symbol** directly to the defining node id.
- `OVERRIDES` uses the SCIP `is_implementation` `Relationship` record (child-method symbol → parent-method symbol), resolving both endpoints by id. The graph-traversal-by-inheritance fallback is **removed**.

With those two gone, no Phase-2 output is an input to any other Phase-2 output. Labels come from each node's own `scip_kind`/source; edges come from SCIP records whose endpoints resolve against Phase-1 data. Therefore everything is computed and written in one pass.

---

## Symbol Resolution

Phase 1 stores the full SCIP symbol string on every node as `scip_descriptor` (and the kind as `scip_kind`). At the start of Phase 2, build a single in-memory index from the repository graph:

```python
# {scip_descriptor (SCIP symbol string): node_id}
symbol_to_id: dict[str, str] = {
    n["scip_descriptor"]: n["id"]
    for n in graph_writer.get_all_symbol_nodes(graph_name)
}
```

Every relationship target — whether it came from a SCIP `Relationship` record, a SCIP `Occurrence`, or the field-type reference used by `Object`/`BELONGS_TO` — is a SCIP symbol string. Resolution is a single dict lookup:

- **Hit** → the target node id; emit the edge.
- **Miss** → the symbol is defined outside this repository → the referenced node is `External` (its stub, if Phase 1 created one, is labeled `External`; otherwise the edge is dropped and logged at debug).

This map is the *only* lookup structure relationships need, and it is derived entirely from Phase-1 properties, so it is stable for the whole pass.

> **Regex inheritance fallback** is the one case that resolves by *name* rather than SCIP symbol (the regex only yields a parent's simple name from source). It still does not read Phase-2 labels — it matches Phase-1 `name` + `scip_kind` class-like buckets via `get_nodes_by_name_and_class_kind`. It is therefore still order-independent.

---

## The Single Pass

For each node (work units may be partitioned by node for SCIP/label work and by file for regex work, and processed in parallel), compute and accumulate **all** of the following, then flush once:

### A. SCIP labels & properties (per node, from `scip_kind` / `scip_descriptor`)

**Definition labels (kind-driven).** Inspect `scip_kind` and apply the label mapping.

**Kind-driven label mapping (illustrative — verify against authoritative SCIP `SymbolKind`; track the protobuf enum verbatim and regenerate bindings from `scip.proto` for production):**

| scip_kind (illustr.) | Equivalent concept | Labels Added |
|------|----------------|-------------------------|
| 2    | Module         | Module                  |
| 5    | Class          | Class                   |
| 6    | Method         | CodeUnit, Method        |
| 7    | Property       | Attribute               |
| 8    | Field          | Attribute               |
| 9    | Constructor    | Constructor             |
| 10   | Enum           | Enum                    |
| 11   | Interface      | Interface               |
| 12   | Function       | CodeUnit, Function      |
| 13   | Variable       | Attribute               |
| 14   | Constant       | Attribute               |
| 22   | EnumMember     | Attribute               |
| 23   | Struct         | Class                   |
| 24   | Event          | Event                   |
| 25   | Operator       | CodeUnit, Method        |

**`Object` label (SCIP).** Marks an attribute-like symbol whose declared type is a reference type (a class/struct), **Java and C++ only** (NOT Python — dynamic typing makes type labels unreliable). Derived from SCIP, without LSP:

1. For each node with `scip_kind` in the Property/Field/Variable/Constant bucket, read the field's **declared type** from the SCIP `SymbolInformation.signature_documentation` / `Relationship` records emitted by the indexer (the field symbol carries a typed relationship / type reference to its declared-type symbol).
2. If the resolved declared-type symbol is a reference type (non-primitive — see exclusion lists), add label `Object`.
3. Set property `reference_type_detail` to the resolved declared-type **SCIP symbol** (used directly by `BELONGS_TO`).

Primitive type exclusion lists are language-specific:
- Java: `boolean`, `byte`, `char`, `short`, `int`, `long`, `float`, `double`, `void`
- C++: `bool`, `char`, `int`, `short`, `long`, `float`, `double`, `void`, `size_t`

**`InnerClass` label (SCIP).** A Class-like symbol whose SCIP descriptor is nested inside another Class-like symbol (equivalently, whose `CONTAINS` parent is a Class) gets `InnerClass`. Deterministic from the SCIP descriptor hierarchy materialized in Phase 1.

**`External` / `Internal` labels (SCIP).** A referenced symbol is **External** when the SCIP index contains references to it but **no definition `Occurrence`** inside this repository (its symbol is absent from `symbol_to_id`, or its node is a Phase-1 reference stub). Label such nodes `External`. Every symbol defined within the repository is `Internal` (the default applied to in-repo symbols).

**Properties set here:**

| Property | Source | Example |
|----------|--------|---------|
| `level` | Derived from primary label (see level table in `Nodes.txt`) | Class → 2, Method → 3 |
| `reference_type_detail` | SCIP declared-type symbol (Object nodes only) | `com.acme.PaymentGateway` |

### B. Regex labels & properties (per file, from source text)

Read local source from **`CodeRepository.local_path` + relative `path`** and apply the per-language strategy's regex rules. No SCIP and no LSP here.

**Regex-based labels:**

| Label                     | Regex Target                          | Source Context Needed  |
|---------------------------|---------------------------------------|-----------------------|
| Destructor                | name: `^~\w+` (C++) or `__del__` (Python) | Node name only    |
| Lambda                    | detail/name: `lambda`, `=>`, `[]() {` | Node detail + source  |
| Abstract                  | `abstract` (Java/TS), `= 0`/`virtual` (C++), `ABC`/`@abstractmethod` (Python) | Source declaration line |
| Testing                   | See language-specific patterns below  | Annotations + name    |
| Accept_call_over_network  | See language-specific patterns below  | Annotations + imports |
| Sends_data_over_network   | See language-specific patterns below  | Body + imports        |
| Database                  | See language-specific patterns below  | Annotations + imports |
| InterProcess Communication | See language-specific patterns below | Body + imports        |
| Thread Communication      | See language-specific patterns below  | Body + imports        |
| Forks Threads / Process   | See language-specific patterns below  | Body + imports        |
| Thread                    | See language-specific patterns below  | Class hierarchy + body |

**Language-specific additive labels:**

| Label          | Condition                   | Language  |
|----------------|-----------------------------|-----------|
| JavaClass      | `scip_kind` maps to Class + language `"java"` | Java only |
| JavaInterface  | `scip_kind` maps to Interface + language `"java"` | Java only |
| JavaEnum       | `scip_kind` maps to Enum + language `"java"` | Java only |

**Properties set here** (regex on declaration lines):

| Property         | How Extracted                                          | Example                              |
|------------------|--------------------------------------------------------|--------------------------------------|
| return_type      | Regex on declaration: return type token                | `public String getName()` → "String"|
| parameter_types  | Regex on declaration: parameter type list              | `(int x, String y)` → ["int","String"] |
| access_modifier  | Regex: `public`/`private`/`protected`/`internal`       | "public"                             |
| modifiers        | Regex: `abstract`, `static`, `final`, `virtual`, `synchronized`, `native`, `volatile` | ["public","static","final"] |
| annotations      | Regex: `@AnnotationName` (Java/Python), `[[attr]]` (C++) | ["@Override","@Test"]             |
| is_static        | Regex: `static` keyword in declaration                 | true / false                         |

### C. SCIP relationships (per source symbol, targets resolved via `symbol_to_id`)

Every edge is `(source_symbol, target_symbol, rel_type)`. The source symbol is the node being processed; the target symbol is resolved through `symbol_to_id`. No Phase-2 label or edge is read.

**`INHERITS` / `IMPLEMENTS`**
1. Prefer native **SCIP `Relationship`** records on each class/interface `SymbolInformation` (the indexer emits the supertype symbols) → resolve each supertype symbol via `symbol_to_id`.
2. **Regex fallback** — when SCIP lacks the edge, apply the documented per-language inheritance regexes (see `Relationships.txt`). The regex yields a parent *name*; resolve via `get_nodes_by_name_and_class_kind` (Phase-1 `name` + class-like `scip_kind`). Ambiguous matches constrain by namespace/package/`path`; unresolved → external.

**`CALLS`** — SCIP emits no explicit call edges, but a reference `Occurrence` to a callable symbol that falls inside the source range of an enclosing callable is a call.
1. Scan SCIP `Occurrence`s whose range lies within the node's `[start_line, end_line]` and whose `SymbolRole` is *not* `Definition`.
2. Keep occurrences whose referenced symbol is itself a callable definition.
3. Resolve the referenced symbol via `symbol_to_id`; emit a deduplicated `(caller)-[:CALLS]->(callee)`.

**`SETS` / `GETS`** — SCIP `Occurrence`s carry `SymbolRole` access bits (`WriteAccess` / `ReadAccess`).
- For each `WriteAccess` occurrence of an attribute symbol → find the enclosing callable by line range → `SETS`.
- For each `ReadAccess` occurrence → find the enclosing callable by line range → `GETS`.
- Enclosing callable is the tightest-span callable node containing the occurrence line, found via `enclosing_callable` (uses Phase-1 `scip_kind`, not Phase-2 labels).

**`OVERRIDES`** — use SCIP `Relationship` records flagged `is_implementation` between a child method symbol and the parent method symbol it overrides; resolve both endpoints via `symbol_to_id`.

> The previous **graph-traversal fallback** (walking `INHERITS`/`IMPLEMENTS` and matching `:Method` by name) is **removed** — it required Phase-2 edges/labels to exist first and broke the single-pass guarantee. When the indexer does not emit `is_implementation`, no `OVERRIDES` edge is produced (logged at debug). Optional refinements still apply to the SCIP-derived candidates: compare `parameter_types`, covariant `return_type`, equal-or-weaker `access_modifier`, and skip if the parent method is `static`.

**`BELONGS_TO`** — for each `Object` node with `reference_type_detail` (a SCIP symbol), resolve that symbol via `symbol_to_id` to the defining Class node and emit `(object)-[:BELONGS_TO]->(class)`. No `:Class` label match, no name match. Unresolved → the type is external (logged at debug).

---

## Write & Rollback

All A/B/C outputs are accumulated, then flushed as one logical Phase-2 transaction. Writes are still grouped into batches for round-trip efficiency (labels grouped by label name, properties unwound, edges grouped by type), but there is **no per-pass ordering** between them.

### Transactional Memcache (Write-Ahead Log)

Every write to FalkorDB during Phase 2 is recorded in an in-memory write-ahead log (WAL). If any batch fails, the entire Phase 2 transaction is rolled back using compensating queries derived from the WAL.

```python
@dataclass
class WriteBatch:
    batch_id: str                                       # Unique identifier for this batch
    labels_added: list[tuple[str, str]]                 # (node_id, label)
    labels_removed: list[tuple[str, str]]               # (node_id, label) -- for Internal->External swap
    properties_set: list[tuple[str, str, Any, Any]]     # (node_id, key, old_value, new_value)
    edges_created: list[tuple[str, str, str]]           # (from_id, to_id, rel_type)

class WriteAheadLog:
    batches: list[WriteBatch]                           # Append-only during Phase 2

    def record(self, batch: WriteBatch): ...
    def rollback_all(self, graph_writer: GraphWriter): ...
    def clear(self): ...
```

On failure, `rollback_all` iterates batches in reverse order, reversing label additions/removals, restoring old property values, and deleting created edges:

```python
def rollback_all(self, graph_writer):
    for batch in reversed(self.batches):
        for node_id, label in batch.labels_added:
            graph_writer.remove_label(node_id, label)
        for node_id, label in batch.labels_removed:
            graph_writer.add_label(node_id, label)
        for node_id, key, old_value, new_value in batch.properties_set:
            graph_writer.set_property(node_id, key, old_value)
        for from_id, to_id, rel_type in batch.edges_created:
            graph_writer.delete_edge(from_id, to_id, rel_type)
```

---

## Deferred Work

- **`SPAWNS`** — thread/process spawn edges are deferred to a later milestone. When implemented they will be regex-driven (per-language strategy) over the source body of nodes labeled `ForksThreadsProcess`, resolving the spawn target to a callable/class node:
  - Java: `new Thread(target)`, `executor.submit(callable)`
  - C++: `std::thread(func)`, `pthread_create(&tid, NULL, func, NULL)`
  - Python: `threading.Thread(target=func)`, `multiprocessing.Process(target=func)`
- **`INSTANTIATES`** — removed from the design. Constructors are represented by the `Constructor` definition label only; no instantiation edge is created.

---

## Design Pattern: Strategy with Common Rule Registry

### Why Strategy over Decorator

- Languages share the SCIP kind-based label mapping (~70% of labels are common across all languages)
- Language-specific logic adds rules rather than wrapping/modifying base logic
- Adding a new language requires one new file, no changes to base or other languages
- Clear rule precedence: common rules run first, language-specific rules extend

### Architecture

```
services/ingestion-worker/src/scip/
    runner.py                   # Thin wrappers around scip-* CLIs per language/repo
    parser.py                   # Protobuf decoding -> intermediate node/edge structures
                                #   (symbols, occurrences, relationships)

services/ingestion-worker/src/crawl/
    phase2_base.py              # Single-pass orchestrator: resolution map, work fan-out,
                                #   write coordination, WAL
    phase2_rules.py             # Rule dataclasses + registry
    strategies/
        __init__.py             # Strategy registry
        common.py               # Shared scip_kind mapping + level
        java.py, cpp.py, python.py, js_ts.py ...
```

There is no `lsp/` package — Phase 2 does not use LSP.

### Rule Dataclasses

```python
@dataclass
class LabelRule:
    label: str                          # Label to add (e.g. "Abstract")
    source: str                         # "scip" | "regex"
    kind_filter: set[int] | None        # SCIP SymbolKind ints this rule applies to (None = all)
    regex_pattern: str | None           # Regex applied to source/detail/name (regex rules only)
    regex_target: str                   # "source", "detail", "name", "annotations"
    languages: set[str] | None          # None = all languages

@dataclass
class RelationshipRule:
    rel_type: str                       # e.g. "INHERITS", "CALLS"
    source: str                         # "scip" | "regex" (regex = inheritance fallback only)
    from_kind_filter: set[int] | None   # Source scip_kind values
    to_kind_filter: set[int] | None     # Target scip_kind values
    regex_pattern: str | None           # Inheritance regex fallback
    languages: set[str] | None          # None = all languages
```

### Strategy Interface

```python
class LanguageStrategy(ABC):
    @abstractmethod
    def scip_label_rules(self) -> list[LabelRule]: ...

    @abstractmethod
    def scip_relationship_rules(self) -> list[RelationshipRule]: ...

    @abstractmethod
    def regex_label_rules(self) -> list[LabelRule]: ...

    @abstractmethod
    def regex_property_extractors(self) -> list[Callable]: ...
```

### Common Strategy (base for all languages)

`common.py` provides the SCIP kind-based label mapping, level assignment, and the `Internal` default. All language strategies inherit from `CommonStrategy`:

```python
class CommonStrategy(LanguageStrategy):
    """Shared rules that work for all languages."""
    def scip_label_rules(self):
        return [
            LabelRule("Class", "scip", {5, 23}, None, "kind", None),
            LabelRule("Interface", "scip", {11}, None, "kind", None),
            LabelRule("CodeUnit", "scip", {6, 12}, None, "kind", None),
            LabelRule("Method", "scip", {6}, None, "kind", None),
            LabelRule("Function", "scip", {12}, None, "kind", None),
            LabelRule("Attribute", "scip", {7, 8, 13, 14, 22}, None, "kind", None),
            LabelRule("Constructor", "scip", {9}, None, "kind", None),
            LabelRule("Enum", "scip", {10}, None, "kind", None),
            LabelRule("Module", "scip", {2}, None, "kind", None),
            LabelRule("Event", "scip", {24}, None, "kind", None),
            LabelRule("Internal", "scip", None, None, "kind", None),
        ]
    # ... scip_relationship_rules, regex_label_rules, regex_property_extractors
```

### Per-Language Strategy (example: Java)

```python
class JavaStrategy(CommonStrategy):
    """Java-specific rules on top of common base."""
    def regex_label_rules(self):
        base = super().regex_label_rules()
        return base + [
            LabelRule("JavaClass", "regex", {5}, None, "kind", {"java"}),
            LabelRule("JavaInterface", "regex", {11}, None, "kind", {"java"}),
            LabelRule("JavaEnum", "regex", {10}, None, "kind", {"java"}),
            LabelRule("Abstract", "regex", {5, 6}, r"\babstract\b", "source", {"java"}),
            LabelRule("Testing", "regex", {5, 6}, r"@Test|@Before|@After|@BeforeEach|@AfterEach", "annotations", {"java"}),
            LabelRule("Accept_call_over_network", "regex", {6}, r"@RequestMapping|@GetMapping|@PostMapping|@PutMapping|@DeleteMapping|@RestController", "annotations", {"java"}),
            # ... more Java-specific regex rules
        ]

    def scip_relationship_rules(self):
        base = super().scip_relationship_rules()
        return base + [
            RelationshipRule("INHERITS", "scip", {5}, {5, 11}, r"class\s+\w+\s+extends\s+(\w+)", {"java"}),
            RelationshipRule("IMPLEMENTS", "scip", {5}, {11}, r"implements\s+([\w,\s]+)", {"java"}),
        ]
```

---

## Language-Specific Classification

### Java

**SCIP labels:** Class, Interface, Enum, CodeUnit, Method, Function, Attribute, Constructor, Event, Object/Instance (reference-type fields via SCIP type relationships), InnerClass, External, Internal

**Regex labels:** Lambda, Abstract, Testing (@Test, @Before, @After, @BeforeEach, @AfterEach), JavaClass, JavaInterface, JavaEnum, Accept_call_over_network (@RequestMapping, @GetMapping, @PostMapping, @PutMapping, @DeleteMapping), Sends_data_over_network (HttpURLConnection, RestTemplate, WebClient, OkHttpClient), Database (@Repository, @Query, JPA annotations, JDBC), Forks Threads/Process (new Thread(), ExecutorService.submit()), Thread (Runnable.run(), Callable.call())

**SCIP relationships:** INHERITS / IMPLEMENTS (SCIP relationships + regex fallback `extends`/`implements`), CALLS (occurrence references), SETS/GETS (occurrence Read/Write roles), OVERRIDES (SCIP `is_implementation`), BELONGS_TO (Object type symbol resolution)

**Deferred:** SPAWNS

### C++

**SCIP labels:** Class, Interface (abstract class with pure virtual methods), Enum, CodeUnit, Method, Function, Attribute, Constructor, Event, Object/Instance (reference-type fields), InnerClass, External, Internal

**Regex labels:** Destructor (~name), Lambda ([](){}), Abstract (= 0, virtual), Testing (TEST(), TEST_F(), TEST_P(), EXPECT_*, ASSERT_*), InterProcess Communication (pipe, shm_open, mq_open), Thread Communication (std::mutex, std::condition_variable, sem_wait), Forks Threads/Process (std::thread, fork(), pthread_create)

**SCIP relationships:** INHERITS (SCIP + regex `: public/protected/private Base`), CALLS, SETS, GETS, OVERRIDES, BELONGS_TO

**Deferred:** SPAWNS

### Python

**SCIP labels:** Class, Module, Enum, CodeUnit, Method, Function, Attribute, Constructor (__init__), Event, InnerClass, External, Internal (NO Object/Instance — dynamic typing makes type labels unreliable)

**Regex labels:** Destructor (__del__), Lambda, Abstract (ABC, @abstractmethod), Testing (test_* function names, @pytest.mark.*, unittest.TestCase subclass), Accept_call_over_network (Flask @app.route, Django urlpatterns, FastAPI @router.get), Sends_data_over_network (requests.*, urllib.request, aiohttp.ClientSession), Database (SQLAlchemy, psycopg2, pymongo, redis), Forks Threads/Process (threading.Thread, multiprocessing.Process, os.fork)

**SCIP relationships:** INHERITS (SCIP + regex `class Foo(Bar)` parenthesized bases), CALLS, SETS, GETS, OVERRIDES (no BELONGS_TO — no Object label in Python)

**Deferred:** SPAWNS

### JavaScript / TypeScript

**SCIP labels:** Class, Interface (TS only), Module, Enum (TS only), CodeUnit, Method, Function, Attribute, Constructor, Object/Instance (TS only, reference-type fields), InnerClass, External, Internal

**Regex labels:** Lambda (arrow =>), Abstract (TS abstract keyword), Testing (describe, it, test, expect from Jest/Mocha/Vitest), Accept_call_over_network (Express app.get/post/put/delete, Fastify route), Sends_data_over_network (fetch, axios.*, XMLHttpRequest, node-fetch), Database (mongoose, knex, sequelize, prisma, TypeORM)

**SCIP relationships:** INHERITS (extends), IMPLEMENTS (TS implements), CALLS, SETS, GETS, OVERRIDES, BELONGS_TO

**Deferred:** SPAWNS

---

## Graph Writer -- Query Methods for Phase 2

These read-only methods support relationship resolution. All helper queries execute inside the repository's FalkorDB **named graph**; because each repository is its own graph, no `repo_name` filter is needed. None of them read Phase-2 labels or edges — they read only Phase-1 properties (`scip_descriptor`, `scip_kind`, `name`, `path`, `start_line`, `end_line`).

| Method                                      | Cypher Pattern                                                                                      | Returns           | Used By          |
|---------------------------------------------|-----------------------------------------------------------------------------------------------------|-------------------|------------------|
| `get_all_symbol_nodes()`                    | `MATCH (n) WHERE n.scip_descriptor IS NOT NULL RETURN n`                                            | list[dict]        | Build `symbol_to_id` |
| `get_node_by_id(nid)`                       | `MATCH (n {id: $nid}) RETURN n`                                                                     | dict or None      | All              |
| `get_contains_parent(nid)`                  | `MATCH (p)-[:CONTAINS]->(n {id: $nid}) RETURN p`                                                    | dict or None      | InnerClass, Object |
| `get_nodes_by_name_and_class_kind(name)`    | `MATCH (n {name:$name}) WHERE n.scip_kind IN $class_kinds RETURN n`                                 | list[dict]        | INHERITS regex fallback |
| `enclosing_callable(path, line)`            | `MATCH (n {path:$path}) WHERE n.scip_kind IN (...) AND n.start_line<=$line AND n.end_line>=$line ORDER BY (n.end_line-n.start_line) ASC LIMIT 1` | dict or None | CALLS, SETS/GETS |

> Removed from the previous design: `get_inherits_parents` and `get_methods_of_class` were used only by the OVERRIDES graph-traversal fallback, which no longer exists. `get_nodes_by_name_and_label` (which matched on a Phase-2 label) is replaced by `get_nodes_by_name_and_class_kind` (matches on Phase-1 `scip_kind`).

### Write Methods

| Method                                  | Cypher Pattern                                                              |
|-----------------------------------------|-----------------------------------------------------------------------------|
| `add_label(nid, label)`                 | `MATCH (n {id: $nid}) SET n:Label`                                          |
| `remove_label(nid, label)`              | `MATCH (n {id: $nid}) REMOVE n:Label`                                       |
| `set_property(nid, key, value)`         | `MATCH (n {id: $nid}) SET n += {key: value}`                               |
| `batch_add_labels(pairs)`               | `UNWIND $ids AS nid MATCH (n {id: nid}) SET n:Label` (per-label batched)    |
| `batch_set_properties(updates)`         | `UNWIND $updates AS u MATCH (n {id: u.id}) SET n += u.props`                |
| `batch_create_edges(edges, rel_type)`   | `UNWIND $edges AS e MATCH (a {id: e.from}), (b {id: e.to}) MERGE (a)-[:TYPE]->(b)` |
| `delete_edge(from_id, to_id, rel_type)` | `MATCH (a {id: $fid})-[r:TYPE]->(b {id: $tid}) DELETE r`                    |

---

## Execution Flow (Complete)

```
Phase 1 (see PHASE1_IMPLEMENTATION.md):
  Multi-threaded file processing -> nodes (CodeNode + file-type labels) + CONTAINS
  Each node carries scip_descriptor (SCIP symbol) + scip_kind
  Write to the repository's FalkorDB graph

Phase 2 (single pass):
  Load LanguageStrategy for the repository language bundle
  Build symbol_to_id = {scip_descriptor: node_id} from get_all_symbol_nodes()
  Initialize WriteAheadLog

  For each node (fan out across threads; partition by node for SCIP, by file for regex):
    SCIP labels   : kind-based definition labels, Object (+reference_type_detail),
                    InnerClass, External/Internal, level
    Regex labels  : modifier/intent labels + scalar properties (return_type,
                    parameter_types, access_modifier, modifiers, annotations, is_static)
    SCIP edges    : INHERITS/IMPLEMENTS (records + regex fallback),
                    CALLS (occurrence-in-range), SETS/GETS (Read/Write roles),
                    OVERRIDES (is_implementation), BELONGS_TO (reference_type_detail)
                    -- every target resolved via symbol_to_id (or, for the
                       inheritance regex fallback, by name + class-like scip_kind)
    -> accumulate labels / properties / edges (no cross-unit reads)

  Flush all accumulated writes to FalkorDB in batches (one logical transaction)
    -> record each batch in WAL

  If any write fails:
    WAL.rollback_all() -> reverse all Phase 2 changes
    Re-raise error

  On success:
    WAL.clear()
    Phase 2 complete

  Deferred: SPAWNS (regex over ForksThreadsProcess bodies) — later milestone
```

---

## References

- Phase 1 design: `PHASE1_IMPLEMENTATION.md`
- Core system design: `core_system/Retrival_system_README.md`
- Node definitions: `Nodes.txt`
- Relationship definitions: `Relationships.txt`
- Repository structure: `repository_structure.md`
