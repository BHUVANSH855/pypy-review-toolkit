"""Tests for scan_gc_hints.py — GC-discipline violation detection.

Covers:
- Pass 1: @rgc.no_collect function with lltype.malloc() → NO_COLLECT_VIOLATION
- Pass 1: @rgc.no_collect function with no allocation → zero findings
- Pass 1: allocation under we_are_translated() guard → NO_COLLECT_SUSPECT or zero
- Pass 2: W_ class with raw lltype.malloc() to self.* and no trace hook → RAW_ALLOC_NO_TRACE_HOOK
- Pass 2: W_ class with raw alloc AND register_custom_trace_hook → zero findings
- Pass 2: W_ class with ffi.new() to self.* and no trace hook → RAW_ALLOC_NO_TRACE_HOOK
- Inline unit tests for the decorator and allocation-call detectors
"""

from __future__ import annotations

import ast
import sys
import textwrap
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO / "plugins" / "pypy-review-toolkit" / "scripts"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gc_hints"

sys.path.insert(0, str(_SCRIPTS))

import scan_gc_hints as _m  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse(source: str) -> ast.Module:
    return ast.parse(textwrap.dedent(source))


def _subtypes(findings: list[dict]) -> list[str]:
    return [f["subtype"] for f in findings if f.get("type") == "gc_hint_violation"]


def _run_fixture(name: str) -> dict:
    """Run analyze() on a single fixture file.

    analyze() filters paths containing '/test/' or '/tests/', so we copy
    the fixture into a temporary directory that has neither in its path
    before scanning.
    """
    import shutil
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / name
        shutil.copy2(_FIXTURES / name, dest)
        return _m.analyze(tmp)


# ---------------------------------------------------------------------------
# Unit tests for internal helpers
# ---------------------------------------------------------------------------

def test_no_collect_decorator_recognized():
    src = """
        import rgc
        @rgc.no_collect
        def f(): pass
    """
    tree = _parse(src)
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    assert funcs, "Expected at least one FunctionDef"
    assert any(_m._is_no_collect_decorator(d) for d in funcs[0].decorator_list)


def test_no_collect_with_trace_recognized():
    src = """
        @rgc.no_collect_with_trace
        def g(): pass
    """
    tree = _parse(src)
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    assert any(_m._is_no_collect_decorator(d) for d in funcs[0].decorator_list)


def test_gc_malloc_detected():
    src = "lltype.malloc(TP)"
    tree = _parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert any(_m._is_gc_malloc(c) for c in calls)


def test_raw_malloc_not_flagged_as_gc():
    src = "lltype.malloc(TP, flavor='raw')"
    tree = _parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert not any(_m._is_gc_malloc(c) for c in calls)


def test_w_class_construction_detected():
    src = "x = W_MyObject(space)"
    tree = _parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert any(_m._is_w_class_construction(c) for c in calls)


def test_raw_malloc_detected():
    src = "lltype.malloc(BUF, flavor='raw')"
    tree = _parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert any(_m._is_raw_malloc(c) for c in calls)


def test_ffi_new_detected():
    src = "p = ffi.new('int *')"
    tree = _parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert any(_m._is_ffi_new(c) for c in calls)


def test_has_trace_hook_true():
    src = textwrap.dedent("""
        class W_Foo(object):
            def setup(self):
                rgc.register_custom_trace_hook(W_Foo, W_Foo._trace)
    """)
    tree = _parse(src)
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
    assert _m._has_trace_hook(cls)


def test_has_trace_hook_false():
    src = textwrap.dedent("""
        class W_Bar(object):
            def setup(self):
                pass
    """)
    tree = _parse(src)
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
    assert not _m._has_trace_hook(cls)


# ---------------------------------------------------------------------------
# Fixture-driven scanner tests
# ---------------------------------------------------------------------------

def test_no_collect_with_malloc_flagged():
    """@rgc.no_collect with lltype.malloc() must produce NO_COLLECT_VIOLATION."""
    result = _run_fixture("no_collect_with_malloc.py")
    findings = result.get("findings", [])
    subtypes = _subtypes(findings)
    assert "NO_COLLECT_VIOLATION" in subtypes, (
        f"Expected NO_COLLECT_VIOLATION; got subtypes={subtypes}"
    )


def test_no_collect_clean_zero_findings():
    """@rgc.no_collect with pure arithmetic must produce zero gc_hint findings."""
    result = _run_fixture("no_collect_clean.py")
    findings = result.get("findings", [])
    gc_findings = [f for f in findings if f.get("type") == "gc_hint_violation"]
    assert gc_findings == [], f"Expected zero GC findings; got {gc_findings}"


def test_no_collect_translated_guard_not_violation():
    """Allocation under if not we_are_translated(): must NOT be NO_COLLECT_VIOLATION.

    Acceptable outcomes: zero findings or NO_COLLECT_SUSPECT.  A
    NO_COLLECT_VIOLATION here would be a false positive — the allocation
    never runs in the translated binary where @no_collect matters.
    """
    result = _run_fixture("no_collect_translated_guard.py")
    findings = result.get("findings", [])
    subtypes = _subtypes(findings)
    assert "NO_COLLECT_VIOLATION" not in subtypes, (
        f"we_are_translated() guard should suppress NO_COLLECT_VIOLATION; "
        f"got subtypes={subtypes}"
    )


def test_raw_alloc_no_hook_flagged():
    """W_ class with raw lltype.malloc() to self.* and no trace hook → RAW_ALLOC_NO_TRACE_HOOK."""
    result = _run_fixture("raw_alloc_no_hook.py")
    findings = result.get("findings", [])
    subtypes = _subtypes(findings)
    assert "RAW_ALLOC_NO_TRACE_HOOK" in subtypes, (
        f"Expected RAW_ALLOC_NO_TRACE_HOOK; got subtypes={subtypes}"
    )


def test_raw_alloc_with_hook_zero_findings():
    """W_ class with trace hook registered must produce zero RAW_ALLOC findings."""
    result = _run_fixture("raw_alloc_with_hook.py")
    findings = result.get("findings", [])
    raw_findings = [f for f in findings if f.get("subtype") in ("RAW_ALLOC_NO_TRACE_HOOK", "RAW_ALLOC_SUSPECT")]
    assert raw_findings == [], f"Expected zero raw-alloc findings; got {raw_findings}"


def test_ffi_alloc_no_hook_flagged():
    """W_ class with ffi.new() assigned to self.ptr and no trace hook → RAW_ALLOC_NO_TRACE_HOOK."""
    result = _run_fixture("w_class_ffi_alloc_no_hook.py")
    findings = result.get("findings", [])
    subtypes = _subtypes(findings)
    assert "RAW_ALLOC_NO_TRACE_HOOK" in subtypes, (
        f"Expected RAW_ALLOC_NO_TRACE_HOOK for ffi.new() case; got subtypes={subtypes}"
    )


# ---------------------------------------------------------------------------
# Envelope structure tests
# ---------------------------------------------------------------------------

def test_envelope_has_pypy_info():
    result = _run_fixture("no_collect_clean.py")
    assert "pypy_info" in result, "Envelope must include pypy_info block"


def test_findings_have_required_fields():
    result = _run_fixture("no_collect_with_malloc.py")
    for f in result.get("findings", []):
        if f.get("type") != "gc_hint_violation":
            continue
        for field in ("subtype", "line", "function", "detail", "classification", "confidence"):
            assert field in f, f"Finding missing field {field!r}: {f}"


def test_inline_no_collect_violation():
    """Inline test: synthetic @no_collect function with list literal is flagged."""
    src = textwrap.dedent("""\
        from rpython.rlib import rgc

        @rgc.no_collect
        def make_list():
            return [1, 2, 3]
    """)
    import tempfile, os
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "sample.py"
        p.write_text(src, encoding="utf-8")
        result = _m.analyze(tmp)
    gc_findings = [f for f in result.get("findings", []) if f.get("type") == "gc_hint_violation"]
    assert gc_findings, "List literal inside @no_collect must produce a finding"


def test_inline_w_class_alloc_name_heuristic():
    """Inline test: callee ending in _alloc inside @no_collect triggers finding."""
    src = textwrap.dedent("""\
        from rpython.rlib import rgc

        @rgc.no_collect
        def setup():
            obj = create_alloc()
            return obj
    """)
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "sample.py"
        p.write_text(src, encoding="utf-8")
        result = _m.analyze(tmp)
    gc_findings = [f for f in result.get("findings", []) if f.get("type") == "gc_hint_violation"]
    assert gc_findings, "Allocator-named call inside @no_collect must produce a finding"
