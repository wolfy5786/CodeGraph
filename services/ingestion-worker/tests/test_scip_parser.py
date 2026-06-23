"""Unit tests for the SCIP parser.

Self-contained: hand-encodes a minimal ``index.scip`` protobuf so the tests
need no SCIP toolchain. Runs under pytest or directly (``python
test_scip_parser.py``).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from scip_parser import (  # noqa: E402
    _parse_descriptors,
    decode_index,
    parse_scip_file,
)

# SCIP SymbolInformation.Kind ordinals used below.
_KIND_CLASS = 7
_KIND_METHOD = 25
_ROLE_DEFINITION = 0x1


# ---------------------------------------------------------------------------
# Minimal protobuf wire encoder (mirror of the decoder under test)
# ---------------------------------------------------------------------------

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _tag(field_no: int, wire: int) -> bytes:
    return _varint((field_no << 3) | wire)


def _len_field(field_no: int, payload: bytes) -> bytes:
    return _tag(field_no, 2) + _varint(len(payload)) + payload


def _str_field(field_no: int, value: str) -> bytes:
    return _len_field(field_no, value.encode("utf-8"))


def _varint_field(field_no: int, value: int) -> bytes:
    return _tag(field_no, 0) + _varint(value)


def _packed(field_no: int, ints: list[int]) -> bytes:
    return _len_field(field_no, b"".join(_varint(i) for i in ints))


def _occurrence(symbol: str, rng: list[int], roles: int, enclosing: list[int]) -> bytes:
    body = _packed(1, rng) + _str_field(2, symbol) + _varint_field(3, roles)
    if enclosing:
        body += _packed(7, enclosing)
    return body


def _symbol_info(symbol: str, kind: int, display_name: str) -> bytes:
    return _str_field(1, symbol) + _varint_field(5, kind) + _str_field(6, display_name)


def _build_synthetic_index() -> bytes:
    """One Python document: top-level MyClass containing method()."""
    class_sym = "scip-python python testpkg 1.0 foo/MyClass#"
    method_sym = "scip-python python testpkg 1.0 foo/MyClass#method()."

    doc = b""
    doc += _str_field(1, "foo.py")                       # relative_path
    doc += _len_field(2, _occurrence(                    # MyClass definition
        class_sym, [2, 6, 13], _ROLE_DEFINITION, [2, 0, 9, 0]))
    doc += _len_field(2, _occurrence(                    # method definition
        method_sym, [5, 8, 14], _ROLE_DEFINITION, [5, 4, 7, 0]))
    doc += _len_field(3, _symbol_info(class_sym, _KIND_CLASS, "MyClass"))
    doc += _len_field(3, _symbol_info(method_sym, _KIND_METHOD, "method"))
    doc += _str_field(4, "python")                       # language

    return _len_field(2, doc)                            # Index.documents[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_decode_index_reads_document() -> None:
    docs = decode_index(_build_synthetic_index())
    assert len(docs) == 1
    doc = docs[0]
    assert doc.relative_path == "foo.py"
    assert doc.language == "python"
    assert len(doc.occurrences) == 2
    assert len(doc.symbols) == 2
    assert all(o.is_definition for o in doc.occurrences)


def test_parse_descriptors_hierarchy() -> None:
    method_sym = "scip-python python testpkg 1.0 foo/MyClass#method()."
    descriptors = _parse_descriptors(method_sym)
    assert [d.name for d in descriptors] == ["foo", "MyClass", "method"]
    assert [d.suffix for d in descriptors] == ["package", "type", "method"]
    # Parent-symbol slicing must reproduce the class symbol exactly.
    parent = method_sym[: descriptors[-1].start]
    assert parent == "scip-python python testpkg 1.0 foo/MyClass#"


def test_parse_descriptors_skips_local() -> None:
    assert _parse_descriptors("local 0") == []


def test_parse_scip_file_builds_nodes_and_edges() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        scip_path = Path(tmp) / "index.scip"
        scip_path.write_bytes(_build_synthetic_index())
        graph = parse_scip_file(str(scip_path), "g", "python")

    by_name = {n.name: n for n in graph.nodes}
    assert set(by_name) == {"MyClass", "method"}

    cls = by_name["MyClass"]
    assert cls.primary_label == "Class"
    assert cls.id == "g:foo.py:3:MyClass"
    assert (cls.start_line, cls.end_line) == (3, 10)   # enclosing_range, 1-based
    assert cls.path == "foo.py"
    assert cls.language == "python"

    method = by_name["method"]
    assert method.primary_label == "Method"
    assert method.id == "g:foo.py:6:method"
    assert (method.start_line, method.end_line) == (6, 8)

    edges = {(e.from_id, e.to_id): e.order for e in graph.edges}
    assert ("g:foo.py", cls.id) in edges            # File → top-level class
    assert (cls.id, method.id) in edges             # class → nested method
    assert graph.parse_errors == 0


def _main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _main()
