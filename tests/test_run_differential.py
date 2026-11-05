"""Tests for scripts/run_differential.py."""

import sys
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent / "plugins" / "pypy-review-toolkit" / "scripts"),
)

from run_differential import differential  # noqa: E402


def _envelope(findings: list[dict]) -> dict:
    """Build a minimal scanner envelope with the given findings."""
    return {"findings": findings}


def _f(file: str, ftype: str, line: int, **extra) -> dict:
    """Convenience finding factory."""
    return {"file": file, "type": ftype, "line": line, **extra}


# ---------------------------------------------------------------------------
# Case 1: one shared finding, one unique in each envelope
# ---------------------------------------------------------------------------

def test_new_resolved_and_unchanged():
    shared = _f("pypy/module/foo.py", "unvalidated-helper-call-range", 42)
    only_in_baseline = _f("pypy/module/bar.py", "unvalidated-helper-call-range", 10)
    only_in_current = _f("pypy/module/baz.py", "unvalidated-helper-call-range", 99)

    baseline = _envelope([shared, only_in_baseline])
    current = _envelope([shared, only_in_current])

    result = differential(baseline, current)

    assert len(result["new"]) == 1, f"Expected 1 new finding; got {result['new']}"
    assert len(result["resolved"]) == 1, f"Expected 1 resolved finding; got {result['resolved']}"
    assert result["unchanged_count"] == 1, (
        f"Expected unchanged_count=1; got {result['unchanged_count']}"
    )
    assert result["new"][0]["file"] == "pypy/module/baz.py"
    assert result["resolved"][0]["file"] == "pypy/module/bar.py"


# ---------------------------------------------------------------------------
# Case 2: identical inputs — everything unchanged
# ---------------------------------------------------------------------------

def test_identical_inputs_all_unchanged():
    findings = [
        _f("pypy/module/a.py", "type-a", 1),
        _f("pypy/module/b.py", "type-b", 2),
        _f("pypy/module/c.py", "type-c", 3),
    ]
    baseline = _envelope(findings)
    current = _envelope(findings)

    result = differential(baseline, current)

    assert result["new"] == [], f"Expected no new findings; got {result['new']}"
    assert result["resolved"] == [], f"Expected no resolved findings; got {result['resolved']}"
    assert result["unchanged_count"] == len(findings), (
        f"Expected unchanged_count={len(findings)}; got {result['unchanged_count']}"
    )


# ---------------------------------------------------------------------------
# Case 3: empty baseline — all current findings are "new"
# ---------------------------------------------------------------------------

def test_empty_baseline_all_new():
    findings = [
        _f("pypy/module/x.py", "type-x", 5),
        _f("pypy/module/y.py", "type-y", 10),
    ]
    baseline = _envelope([])
    current = _envelope(findings)

    result = differential(baseline, current)

    assert len(result["new"]) == len(findings), (
        f"Expected {len(findings)} new findings; got {result['new']}"
    )
    assert result["resolved"] == [], f"Expected no resolved findings; got {result['resolved']}"
    assert result["unchanged_count"] == 0


# ---------------------------------------------------------------------------
# Case 4: same finding at different lines → both new and resolved
# ---------------------------------------------------------------------------

def test_line_change_counts_as_new_and_resolved():
    old = _f("pypy/module/foo.py", "unvalidated-helper-call-range", 42)
    new = _f("pypy/module/foo.py", "unvalidated-helper-call-range", 43)

    result = differential(_envelope([old]), _envelope([new]))

    assert len(result["new"]) == 1
    assert len(result["resolved"]) == 1
    assert result["unchanged_count"] == 0


# ---------------------------------------------------------------------------
# Case 5: empty current — all baseline findings are "resolved"
# ---------------------------------------------------------------------------

def test_empty_current_all_resolved():
    findings = [_f("pypy/module/z.py", "type-z", 7)]
    result = differential(_envelope(findings), _envelope([]))

    assert result["new"] == []
    assert len(result["resolved"]) == 1
    assert result["unchanged_count"] == 0
