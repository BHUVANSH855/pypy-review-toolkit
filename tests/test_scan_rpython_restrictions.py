"""Tests for scan_rpython_restrictions.py."""

import shutil
import sys
import tempfile
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "pypy-review-toolkit" / "scripts"))

from scan_rpython_restrictions import _check_file, analyze as _analyze  # noqa: E402


def _write(tmp_path: Path, source: str) -> Path:
    f = tmp_path / "sample.py"
    f.write_text(textwrap.dedent(source))
    return f


def test_kwargs_function_is_flagged(tmp_path):
    src = """
        def f(a, b, **kwds):
            return kwds
    """
    f = _write(tmp_path, src)
    findings = _check_file(f, tmp_path)
    assert len(findings) == 1
    assert findings[0]["type"] == "rpython-unbounded-kwargs"


def test_not_rpython_function_is_not_flagged(tmp_path):
    src = """
        def f(**kwds):
            \"\"\"NOT_RPYTHON\"\"\"
            return kwds
    """
    f = _write(tmp_path, src)
    findings = _check_file(f, tmp_path)
    assert findings == []


def test_not_rpython_module_is_not_flagged(tmp_path):
    src = """
        # NOT_RPYTHON

        def f(**kwds):
            return kwds
    """
    f = _write(tmp_path, src)
    findings = _check_file(f, tmp_path)
    assert findings == []


def test_function_without_kwargs_not_flagged(tmp_path):
    src = """
        def f(a, b):
            return a + b
    """
    f = _write(tmp_path, src)
    findings = _check_file(f, tmp_path)
    assert findings == []


def test_eval_exec_not_flagged_at_all(tmp_path):
    # Deliberately excluded per the design doc's investigation -- 0 of 21
    # real candidates were genuine violations.
    src = """
        def f():
            exec("x = 1")
            return eval("1 + 1")
    """
    f = _write(tmp_path, src)
    findings = _check_file(f, tmp_path)
    assert findings == []


def test_not_rpython_decorator_is_not_flagged(tmp_path):
    src = """
        def not_rpython(func):
            return func

        @not_rpython
        def f(**kwds):
            return kwds
    """
    f = _write(tmp_path, src)
    findings = _check_file(f, tmp_path)
    assert findings == []


# ---------------------------------------------------------------------------
# RPYTHON_MIXED_CONTAINER and RPYTHON_GENERATOR_EXPR tests (Issue #19 — Gap 3)
# ---------------------------------------------------------------------------

_RR_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "rpython_restrictions"


def _run_rr_fixture(name: str):
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / name
        shutil.copy2(_RR_FIXTURES / name, dest)
        return _analyze(tmp)


def test_mixed_int_str_list_flagged():
    """A list literal with mixed int and str constants triggers RPYTHON_MIXED_CONTAINER."""
    result = _run_rr_fixture("mixed_int_str_list.py")
    types = [f["type"] for f in result.get("findings", [])]
    assert "rpython-mixed-container" in types, (
        f"Expected rpython-mixed-container in {types}"
    )


def test_generator_expr_flagged():
    """A generator expression triggers RPYTHON_GENERATOR_EXPR."""
    result = _run_rr_fixture("generator_expr.py")
    types = [f["type"] for f in result.get("findings", [])]
    assert "rpython-generator-expr" in types, (
        f"Expected rpython-generator-expr in {types}"
    )


def test_list_comprehension_not_flagged():
    """A list comprehension (OK in RPython) produces no rpython-* findings."""
    result = _run_rr_fixture("list_comp_ok.py")
    rpython_findings = [
        f for f in result.get("findings", [])
        if str(f.get("type", "")).startswith("rpython-")
    ]
    assert rpython_findings == [], (
        f"Did not expect rpython-* findings for list comprehension; got {rpython_findings}"
    )
