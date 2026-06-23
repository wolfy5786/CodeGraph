"""SCIP `index.scip` decoder and Phase 1 code-symbol node/edge builder.

This module turns the protobuf emitted by a SCIP indexer into the
code-symbol nodes and ``CONTAINS`` edges described in
``documentation/PHASE1_IMPLEMENTATION.md``.

Why a hand-rolled protobuf reader
---------------------------------
The SCIP schema we consume is tiny and stable (``Index`` → ``Document`` →
``Occurrence`` / ``SymbolInformation``). Vendoring a ``protoc``-generated
module would add a build-time ``protoc`` dependency that the local-first
desktop prototype otherwise does not need, so we decode the handful of
wire fields we care about directly. Field numbers below mirror the
upstream ``scip.proto`` (github.com/sourcegraph/scip).

The reader is deliberately tolerant: any single document or symbol that
fails to decode is logged and skipped rather than aborting the whole
Phase 1 write.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)
_SERVICE = "scip_parser"

# SCIP SymbolRole bitmask — only Definition matters for node placement.
_ROLE_DEFINITION = 0x1


# ---------------------------------------------------------------------------
# Low-level protobuf wire reader (only the subset SCIP uses)
# ---------------------------------------------------------------------------

_WIRE_VARINT = 0
_WIRE_64BIT = 1
_WIRE_LEN = 2
_WIRE_32BIT = 5


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Decode a base-128 varint at ``pos``; return ``(value, new_pos)``."""
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _iter_fields(buf: bytes) -> Iterator[tuple[int, int, Any]]:
    """Yield ``(field_number, wire_type, payload)`` for every field in ``buf``.

    ``payload`` is an ``int`` for varint fields and ``bytes`` for
    length-delimited fields. 32-/64-bit fixed fields are skipped (SCIP does
    not use them in the fields we read) but consumed so iteration stays aligned.
    """
    pos = 0
    n = len(buf)
    while pos < n:
        key, pos = _read_varint(buf, pos)
        field_no = key >> 3
        wire = key & 0x7
        if wire == _WIRE_VARINT:
            value, pos = _read_varint(buf, pos)
            yield field_no, wire, value
        elif wire == _WIRE_LEN:
            length, pos = _read_varint(buf, pos)
            value = buf[pos:pos + length]
            pos += length
            yield field_no, wire, value
        elif wire == _WIRE_64BIT:
            pos += 8
        elif wire == _WIRE_32BIT:
            pos += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")


def _read_packed_varints(payload: bytes) -> list[int]:
    """Decode a packed-repeated int32 field (SCIP ``range``/``enclosing_range``)."""
    out: list[int] = []
    pos = 0
    while pos < len(payload):
        value, pos = _read_varint(payload, pos)
        out.append(value)
    return out


# ---------------------------------------------------------------------------
# Decoded SCIP message shapes (only the fields Phase 1 needs)
# ---------------------------------------------------------------------------

@dataclass
class ScipOccurrence:
    symbol: str
    roles: int
    rng: list[int]              # [startLine, startChar, endChar] or [sl, sc, el, ec]
    enclosing_range: list[int]  # full-definition span, may be empty

    @property
    def is_definition(self) -> bool:
        return bool(self.roles & _ROLE_DEFINITION)


@dataclass
class ScipSymbolInfo:
    symbol: str
    kind: int
    display_name: str
    enclosing_symbol: str


@dataclass
class ScipDocument:
    relative_path: str
    language: str
    occurrences: list[ScipOccurrence] = field(default_factory=list)
    symbols: list[ScipSymbolInfo] = field(default_factory=list)


def _decode_occurrence(buf: bytes) -> ScipOccurrence:
    rng: list[int] = []
    enclosing: list[int] = []
    symbol = ""
    roles = 0
    for field_no, wire, payload in _iter_fields(buf):
        if field_no == 1:  # range
            if wire == _WIRE_LEN:
                rng.extend(_read_packed_varints(payload))
            else:
                rng.append(payload)
        elif field_no == 2 and wire == _WIRE_LEN:  # symbol
            symbol = payload.decode("utf-8", "replace")
        elif field_no == 3 and wire == _WIRE_VARINT:  # symbol_roles
            roles = payload
        elif field_no == 7:  # enclosing_range
            if wire == _WIRE_LEN:
                enclosing.extend(_read_packed_varints(payload))
            else:
                enclosing.append(payload)
    return ScipOccurrence(symbol=symbol, roles=roles, rng=rng, enclosing_range=enclosing)


def _decode_symbol_info(buf: bytes) -> ScipSymbolInfo:
    symbol = ""
    kind = 0
    display_name = ""
    enclosing_symbol = ""
    for field_no, wire, payload in _iter_fields(buf):
        if field_no == 1 and wire == _WIRE_LEN:
            symbol = payload.decode("utf-8", "replace")
        elif field_no == 5 and wire == _WIRE_VARINT:  # kind
            kind = payload
        elif field_no == 6 and wire == _WIRE_LEN:  # display_name
            display_name = payload.decode("utf-8", "replace")
        elif field_no == 8 and wire == _WIRE_LEN:  # enclosing_symbol
            enclosing_symbol = payload.decode("utf-8", "replace")
    return ScipSymbolInfo(
        symbol=symbol, kind=kind, display_name=display_name,
        enclosing_symbol=enclosing_symbol,
    )


def _decode_document(buf: bytes) -> ScipDocument:
    relative_path = ""
    language = ""
    occurrences: list[ScipOccurrence] = []
    symbols: list[ScipSymbolInfo] = []
    for field_no, wire, payload in _iter_fields(buf):
        if field_no == 1 and wire == _WIRE_LEN:  # relative_path
            relative_path = payload.decode("utf-8", "replace")
        elif field_no == 2 and wire == _WIRE_LEN:  # occurrence
            occurrences.append(_decode_occurrence(payload))
        elif field_no == 3 and wire == _WIRE_LEN:  # symbol
            symbols.append(_decode_symbol_info(payload))
        elif field_no == 4 and wire == _WIRE_LEN:  # language
            language = payload.decode("utf-8", "replace")
    return ScipDocument(
        relative_path=relative_path, language=language,
        occurrences=occurrences, symbols=symbols,
    )


def decode_index(buf: bytes) -> list[ScipDocument]:
    """Decode a raw ``index.scip`` byte string into its documents."""
    documents: list[ScipDocument] = []
    for field_no, wire, payload in _iter_fields(buf):
        if field_no == 2 and wire == _WIRE_LEN:  # Index.documents
            documents.append(_decode_document(payload))
    return documents


# ---------------------------------------------------------------------------
# SCIP symbol-string descriptor parsing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Descriptor:
    name: str
    suffix: str   # one of: package type term method meta macro typeparam parameter
    start: int    # offset of this descriptor within the full symbol string


# SCIP descriptor suffix characters → descriptor kind.
_SUFFIX_KIND = {"/": "package", "#": "type", ".": "term", ":": "meta", "!": "macro"}


def _skip_meta_fields(symbol: str, count: int) -> int:
    """Return the index where descriptors begin, after ``count`` meta fields.

    The first fields of a SCIP symbol (scheme, manager, package name, version)
    are space-delimited; a literal space inside one is escaped by doubling it.
    """
    i = 0
    seen = 0
    n = len(symbol)
    while i < n and seen < count:
        if symbol[i] == " ":
            if i + 1 < n and symbol[i + 1] == " ":
                i += 2          # escaped literal space
                continue
            seen += 1
        i += 1
    return i


def _read_descriptor_name(desc: str, i: int) -> tuple[str, int]:
    """Read a (possibly backtick-escaped) descriptor name starting at ``i``."""
    if i < len(desc) and desc[i] == "`":
        out = []
        i += 1
        while i < len(desc):
            if desc[i] == "`":
                if i + 1 < len(desc) and desc[i + 1] == "`":
                    out.append("`")
                    i += 2
                    continue
                i += 1
                break
            out.append(desc[i])
            i += 1
        return "".join(out), i
    start = i
    while i < len(desc) and desc[i] not in "#./:!(":
        i += 1
    return desc[start:i], i


def _parse_descriptors(symbol: str) -> list[_Descriptor]:
    """Split a global SCIP symbol into its ordered descriptors.

    Local symbols (``"local 1"``) and malformed inputs yield an empty list.
    """
    if not symbol or symbol.startswith("local "):
        return []
    base = _skip_meta_fields(symbol, 4)
    desc = symbol[base:]
    out: list[_Descriptor] = []
    i = 0
    n = len(desc)
    while i < n:
        start = i
        char = desc[i]
        if char == "[":            # type parameter [name]
            name, i = _read_descriptor_name(desc, i + 1)
            if i < n and desc[i] == "]":
                i += 1
            out.append(_Descriptor(name, "typeparam", base + start))
            continue
        if char == "(":            # parameter (name)
            name, i = _read_descriptor_name(desc, i + 1)
            if i < n and desc[i] == ")":
                i += 1
            out.append(_Descriptor(name, "parameter", base + start))
            continue
        name, i = _read_descriptor_name(desc, i)
        if i >= n:
            break
        suffix_char = desc[i]
        if suffix_char == "(":     # method name(disambiguator).
            depth = 1
            i += 1
            while i < n and depth:
                if desc[i] == "(":
                    depth += 1
                elif desc[i] == ")":
                    depth -= 1
                i += 1
            if i < n and desc[i] == ".":
                i += 1
            out.append(_Descriptor(name, "method", base + start))
            continue
        i += 1
        out.append(_Descriptor(name, _SUFFIX_KIND.get(suffix_char, "term"), base + start))
    return out


# ---------------------------------------------------------------------------
# SCIP kind / descriptor → Phase 1 definition label
# ---------------------------------------------------------------------------

# Subset of SCIP SymbolInformation.Kind ordinals → Phase 1 definition labels.
# Only kinds that map to a node label are listed; everything else falls back
# to the descriptor suffix.
_KIND_LABEL: dict[int, str] = {
    27: "Module", 28: "Module", 33: "Module", 34: "Module",          # Module/Namespace/Package
    7: "Class", 46: "Class", 31: "Class", 75: "Class",               # Class/Struct/Object/SingletonClass
    52: "Class", 53: "Class",                                        # Type/TypeAlias
    20: "Interface", 40: "Interface", 50: "Interface", 54: "Interface",  # Interface/Protocol/Trait/TypeClass
    11: "Enum",                                                       # Enum
    9: "Constructor",                                                 # Constructor
    16: "Function",                                                   # Function
    13: "Event",                                                      # Event
    # Methods (incl. accessors / specialised method kinds)
    25: "Method", 17: "Method", 43: "Method", 51: "Method", 55: "Method",
    64: "Method", 65: "Method", 66: "Method", 67: "Method", 70: "Method",
    72: "Method", 74: "Method", 76: "Method",
    # Fields / values / properties
    4: "Attribute", 8: "Attribute", 12: "Attribute", 15: "Attribute",
    21: "Attribute", 39: "Attribute", 59: "Attribute", 60: "Attribute",
}

# Descriptor kinds that should never become standalone nodes.
_NON_NODE_DESCRIPTORS = frozenset({"parameter", "typeparam", "meta"})


def _label_from_descriptor(descriptors: list[_Descriptor]) -> str:
    """Best-effort label when SCIP ``kind`` is unspecified (0)."""
    if not descriptors:
        return "Attribute"
    last = descriptors[-1]
    parent_is_type = len(descriptors) >= 2 and descriptors[-2].suffix == "type"
    if last.suffix == "package":
        return "Module"
    if last.suffix == "type":
        return "Class"
    if last.suffix == "macro":
        return "Function"
    if last.suffix == "method":
        return "Method" if parent_is_type else "Function"
    return "Attribute" if parent_is_type else "Function"  # term


def _resolve_label(kind: int, descriptors: list[_Descriptor]) -> str:
    mapped = _KIND_LABEL.get(kind)
    if mapped:
        return mapped
    return _label_from_descriptor(descriptors)


# ---------------------------------------------------------------------------
# Range helpers
# ---------------------------------------------------------------------------

def _span_lines(rng: list[int], enclosing: list[int]) -> tuple[int, int]:
    """Return 1-based inclusive (start_line, end_line) for a definition.

    Prefers ``enclosing_range`` (the full definition body) when present,
    otherwise uses the occurrence ``range`` (just the identifier). SCIP line
    numbers are 0-based and encoding-independent, so only +1 is needed.
    """
    source = enclosing if len(enclosing) >= 3 else rng
    if not source:
        return 1, 1
    start = source[0]
    end = source[2] if len(source) >= 4 else source[0]
    return start + 1, end + 1


# ---------------------------------------------------------------------------
# Phase 1 node / edge construction
# ---------------------------------------------------------------------------

@dataclass
class CodeSymbolNode:
    """A SCIP-derived code-symbol node ready for GraphWriter."""

    id: str
    name: str
    path: str
    language: str
    start_line: int
    end_line: int
    scip_descriptor: str
    scip_kind: int
    primary_label: str
    signature: str = ""   # empty until Phase 2 enrichment (per design)
    detail: str = ""

    def to_props(self, graph_name: str, repo_name: str) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "language": self.language,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "scip_descriptor": self.scip_descriptor,
            "scip_kind": self.scip_kind,
            "signature": self.signature,
            "detail": self.detail,
            "graph_name": graph_name,
            "repo_name": repo_name,
        }


@dataclass
class ContainsEdge:
    from_id: str
    to_id: str
    order: int


@dataclass
class Phase1Graph:
    """Code-symbol nodes and CONTAINS edges parsed from one or more indexes."""

    nodes: list[CodeSymbolNode] = field(default_factory=list)
    edges: list[ContainsEdge] = field(default_factory=list)
    documents: int = 0
    parse_errors: int = 0


def _build_document_nodes(
    graph_name: str,
    doc: ScipDocument,
    default_language: str,
) -> tuple[list[CodeSymbolNode], list[ContainsEdge]]:
    """Build code-symbol nodes and CONTAINS edges for one SCIP document."""
    rel_path = doc.relative_path
    language = doc.language or default_language
    file_id = f"{graph_name}:{rel_path}"

    # First definition occurrence per symbol gives its line span.
    spans: dict[str, tuple[int, int]] = {}
    for occ in doc.occurrences:
        if occ.is_definition and occ.symbol and occ.symbol not in spans:
            spans[occ.symbol] = _span_lines(occ.rng, occ.enclosing_range)

    kinds = {s.symbol: s.kind for s in doc.symbols}
    names = {s.symbol: s.display_name for s in doc.symbols}

    nodes: dict[str, CodeSymbolNode] = {}
    parent_symbol: dict[str, str | None] = {}

    for symbol, (start_line, end_line) in spans.items():
        descriptors = _parse_descriptors(symbol)
        if not descriptors or descriptors[-1].suffix in _NON_NODE_DESCRIPTORS:
            continue  # locals, parameters, type parameters are not Phase 1 nodes
        name = names.get(symbol) or descriptors[-1].name
        if not name:
            continue
        label = _resolve_label(kinds.get(symbol, 0), descriptors)
        node_id = f"{graph_name}:{rel_path}:{start_line}:{name}"
        nodes[symbol] = CodeSymbolNode(
            id=node_id,
            name=name,
            path=rel_path,
            language=language,
            start_line=start_line,
            end_line=end_line,
            scip_descriptor=symbol,
            scip_kind=kinds.get(symbol, 0),
            primary_label=label,
        )
        # Parent symbol string is the prefix up to the last descriptor's start.
        prefix = symbol[: descriptors[-1].start]
        parent_symbol[symbol] = prefix if prefix in spans else None

    # Order siblings by start_line for the CONTAINS `order` property.
    edges: list[ContainsEdge] = []
    children_by_parent: dict[str, list[str]] = {}
    for symbol, node in nodes.items():
        parent = parent_symbol.get(symbol)
        anchor = nodes[parent].id if parent and parent in nodes else file_id
        children_by_parent.setdefault(anchor, []).append(symbol)

    for anchor, child_symbols in children_by_parent.items():
        ordered = sorted(child_symbols, key=lambda s: nodes[s].start_line)
        for order, child in enumerate(ordered):
            edges.append(ContainsEdge(from_id=anchor, to_id=nodes[child].id, order=order))

    return list(nodes.values()), edges


def parse_scip_file(
    scip_path: str,
    graph_name: str,
    default_language: str,
) -> Phase1Graph:
    """Decode an ``index.scip`` file into a :class:`Phase1Graph`.

    Per-document failures are logged and skipped; a failure to read or decode
    the whole file raises so the caller can record the run as failed.
    """
    try:
        raw = Path(scip_path).read_bytes()
    except OSError as err:
        logger.error(
            "Could not read SCIP index file",
            extra={"service": _SERVICE, "scip_path": scip_path, "error": str(err)},
        )
        raise

    documents = decode_index(raw)
    result = Phase1Graph(documents=len(documents))
    seen_ids: set[str] = set()

    for doc in documents:
        try:
            nodes, edges = _build_document_nodes(graph_name, doc, default_language)
        except Exception as err:  # noqa: BLE001 — isolate one bad document
            result.parse_errors += 1
            logger.error(
                "Failed to build nodes for SCIP document",
                extra={
                    "service": _SERVICE,
                    "graph": graph_name,
                    "relative_path": doc.relative_path,
                    "error": str(err),
                },
            )
            continue
        for node in nodes:
            if node.id in seen_ids:
                continue
            seen_ids.add(node.id)
            result.nodes.append(node)
        result.edges.extend(edges)

    logger.info(
        "Parsed SCIP index",
        extra={
            "service": _SERVICE,
            "graph": graph_name,
            "scip_path": scip_path,
            "documents": result.documents,
            "nodes": len(result.nodes),
            "edges": len(result.edges),
            "parse_errors": result.parse_errors,
        },
    )
    return result
