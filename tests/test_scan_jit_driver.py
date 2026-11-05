"""Tests for scan_jit_driver.py — structural JIT violation detection.

Covers:
- Pass 1: JitDriver arg mismatch → JITDRIVER_ARG_MISMATCH
- Pass 1: matching declaration and call site → zero findings
- Pass 2: @jit.unroll_safe with range(len(param)) → UNROLL_SAFE_UNBOUNDED
- Pass 2: @jit.unroll_safe with jit.promote(len(param)) → zero findings
- Pass 3: JitDriver with no jit_merge_point in file → DEAD_JITDRIVER
- Pass 3: JitDriver with jit_merge_point present → zero findings
- Unit tests for internal helpers
"""

from __future__ import annotations

import ast
import shutil
import sys
import tempfile
import textwrap
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO / "plugins" / "pypy-review-toolkit" / "scripts"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jit_driver"

sys.path.insert(0, str(_SCRIPTS))

import scan_jit_driver as _m  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse(source: str) -> ast.Module:
    return ast.parse(textwrap.dedent(source))


def _subtypes(findings: list[dict]) -> list[str]:
    return [f["subtype"] for f in findings if f.get("type") == "jit_driver_issue"]


def _run_fixture(name: str) -> dict:
    """Copy fixture to a temp dir outside 'tests/' so analyze() doesn't filter it."""
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / name
        shutil.copy2(_FIXTURES / name, dest)
        return _m.analyze(tmp)


# ---------------------------------------------------------------------------
# Unit tests for internal helpers
# ---------------------------------------------------------------------------

def test_extract_string_list_basic():
    tree = _parse("x = ['a', 'b', 'c']")
    lst = tree.body[0].value
    result = _m._extract_string_list(lst)
    assert result == ["a", "b", "c"]


def test_extract_string_list_non_literal_returns_none():
    tree = _parse("x = [a, 'b']")
    lst = tree.body[0].value
    result = _m._extract_string_list(lst)
    assert result is None


def test_find_jitdriver_defs_basic():
    src = "jitdriver = JitDriver(greens=['x'], reds=['y'])"
    tree = _parse(src)
    defs = _m._find_jitdriver_defs(tree)
    assert "jitdriver" in defs
    greens, reds, _ = defs["jitdriver"]
    assert greens == ["x"]
    assert reds == ["y"]


def test_find_jitdriver_defs_auto_reds():
    src = "driver = JitDriver(greens=['x'], reds='auto')"
    tree = _parse(src)
    defs = _m._find_jitdriver_defs(tree)
    assert "driver" in defs
    _, reds, _ = defs["driver"]
    assert reds == []


def test_is_unroll_safe_decorator_jit_attr():
    src = "@jit.unroll_safe\ndef f(): pass"
    tree = _parse(src)
    func = tree.body[0]
    assert any(_m._is_unroll_safe_decorator(d) for d in func.decorator_list)


def test_is_unroll_safe_decorator_bare():
    src = "@unroll_safe\ndef f(): pass"
    tree = _parse(src)
    func = tree.body[0]
    assert any(_m._is_unroll_safe_decorator(d) for d in func.decorator_list)


def test_promoted_names():
    src = textwrap.dedent("""\
        def f(lst):
            n = jit.promote(len(lst))
            return n
    """)
    tree = _parse(src)
    func = tree.body[0]
    promoted = _m._promoted_names(func)
    assert "n" in promoted


def test_param_names():
    src = "def f(a, b, c=1): pass"
    tree = _parse(src)
    func = tree.body[0]
    params = _m._param_names(func)
    assert {"a", "b", "c"} <= params


def test_iter_is_range_len_param_true():
    src = "range(len(lst))"
    tree = _parse(src)
    call = tree.body[0].value
    assert _m._iter_is_range_len_param(call, {"lst"}, set())


def test_iter_is_range_len_param_promoted_false():
    src = "range(len(lst))"
    tree = _parse(src)
    call = tree.body[0].value
    # lst is promoted — should NOT be flagged
    assert not _m._iter_is_range_len_param(call, {"lst"}, {"lst"})


# ---------------------------------------------------------------------------
# Fixture-driven scanner tests
# ---------------------------------------------------------------------------

def test_arg_mismatch_flagged():
    """JitDriver call with wrong keyword args must produce JITDRIVER_ARG_MISMATCH."""
    result = _run_fixture("jitdriver_arg_mismatch.py")
    subtypes = _subtypes(result.get("findings", []))
    assert "JITDRIVER_ARG_MISMATCH" in subtypes, (
        f"Expected JITDRIVER_ARG_MISMATCH; got {subtypes}"
    )


def test_jitdriver_clean_zero_findings():
    """Matching declaration and call site must produce zero findings."""
    result = _run_fixture("jitdriver_clean.py")
    gc_findings = [f for f in result.get("findings", []) if f.get("type") == "jit_driver_issue"]
    assert gc_findings == [], f"Expected zero findings; got {gc_findings}"


def test_unroll_safe_unbounded_flagged():
    """@jit.unroll_safe with range(len(param)) must produce UNROLL_SAFE_UNBOUNDED."""
    result = _run_fixture("unroll_safe_unbounded.py")
    subtypes = _subtypes(result.get("findings", []))
    assert "UNROLL_SAFE_UNBOUNDED" in subtypes, (
        f"Expected UNROLL_SAFE_UNBOUNDED; got {subtypes}"
    )


def test_unroll_safe_promoted_zero_findings():
    """@jit.unroll_safe with promoted length must produce zero UNROLL_SAFE findings."""
    result = _run_fixture("unroll_safe_promoted.py")
    unroll_findings = [
        f for f in result.get("findings", [])
        if f.get("subtype") == "UNROLL_SAFE_UNBOUNDED"
    ]
    assert unroll_findings == [], f"Expected zero unroll findings; got {unroll_findings}"


def test_dead_jitdriver_flagged():
    """JitDriver with no jit_merge_point in file must produce DEAD_JITDRIVER."""
    result = _run_fixture("dead_jitdriver.py")
    subtypes = _subtypes(result.get("findings", []))
    assert "DEAD_JITDRIVER" in subtypes, (
        f"Expected DEAD_JITDRIVER; got {subtypes}"
    )


def test_live_jitdriver_zero_findings():
    """JitDriver with jit_merge_point present must produce zero DEAD_JITDRIVER findings."""
    result = _run_fixture("live_jitdriver.py")
    dead_findings = [
        f for f in result.get("findings", [])
        if f.get("subtype") == "DEAD_JITDRIVER"
    ]
    assert dead_findings == [], f"Expected zero dead findings; got {dead_findings}"


# ---------------------------------------------------------------------------
# Envelope structure tests
# ---------------------------------------------------------------------------

def test_envelope_has_pypy_info():
    result = _run_fixture("jitdriver_clean.py")
    assert "pypy_info" in result, "Envelope must include pypy_info block"


def test_findings_have_required_fields():
    result = _run_fixture("jitdriver_arg_mismatch.py")
    for f in result.get("findings", []):
        if f.get("type") != "jit_driver_issue":
            continue
        for field in ("subtype", "line", "driver_var", "detail", "classification", "confidence"):
            assert field in f, f"Finding missing field {field!r}: {f}"


# ---------------------------------------------------------------------------
# Inline tests
# ---------------------------------------------------------------------------

def test_inline_bare_param_iteration():
    """for item in lst where lst is a param must trigger UNROLL_SAFE_UNBOUNDED."""
    src = textwrap.dedent("""\
        from rpython.rlib import jit

        @jit.unroll_safe
        def process(lst):
            result = 0
            for item in lst:
                result += item
            return result
    """)
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "sample.py").write_text(src, encoding="utf-8")
        result = _m.analyze(tmp)
    subtypes = _subtypes(result.get("findings", []))
    assert "UNROLL_SAFE_UNBOUNDED" in subtypes, (
        f"bare 'for item in param' should be UNROLL_SAFE_UNBOUNDED; got {subtypes}"
    )


def test_inline_mismatch_missing_red():
    """Call site missing a declared red must be flagged."""
    src = textwrap.dedent("""\
        from rpython.rlib.jit import JitDriver

        driver = JitDriver(greens=["code"], reds=["pc", "frame"])

        def run(code, pc, frame):
            while True:
                # frame is missing
                driver.jit_merge_point(code=code, pc=pc)
                pc += 1
                if pc >= len(code):
                    break
    """)
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "sample.py").write_text(src, encoding="utf-8")
        result = _m.analyze(tmp)
    subtypes = _subtypes(result.get("findings", []))
    assert "JITDRIVER_ARG_MISMATCH" in subtypes
