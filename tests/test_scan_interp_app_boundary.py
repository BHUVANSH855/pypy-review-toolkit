"""Tests for scan_interp_app_boundary.py."""

import shutil
import sys
import tempfile
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "pypy-review-toolkit" / "scripts"))

from scan_interp_app_boundary import _check_file, _count_callers, analyze as _analyze  # noqa: E402


def _write(tmp_path: Path, source: str) -> Path:
    f = tmp_path / "sample.py"
    f.write_text(textwrap.dedent(source))
    return f


def test_same_function_asymmetry_is_flagged_high_confidence(tmp_path):
    # The pyframe.py:236 shape: one correct check, one raw raise, same function.
    src = """
        def initialize_frame_scopes(self):
            if bad:
                raise oefmt(space.w_TypeError, "bad")
            if closure_size != nfreevars:
                raise ValueError("mismatched closure")
    """
    f = _write(tmp_path, src)
    findings = _check_file(f, tmp_path)
    assert len(findings) == 1
    assert findings[0]["type"] == "interp-app-boundary-same-function-asymmetry"
    assert findings[0]["confidence"] == "high"


def test_raw_exception_alone_is_lower_confidence(tmp_path):
    src = """
        def fixedunpack(self, argcount):
            if self.keywords:
                raise ValueError("no keyword arguments expected")
    """
    f = _write(tmp_path, src)
    findings = _check_file(f, tmp_path)
    assert len(findings) == 1
    assert findings[0]["type"] == "interp-app-boundary-raw-exception"
    assert findings[0]["confidence"] == "low"


def test_operror_only_function_no_findings(tmp_path):
    src = """
        def f(self):
            raise oefmt(space.w_TypeError, "bad")
    """
    f = _write(tmp_path, src)
    findings = _check_file(f, tmp_path)
    assert findings == []


def test_irrelevant_exception_name_ignored(tmp_path):
    src = """
        def f(self):
            raise SomeCustomThing("bad")
    """
    f = _write(tmp_path, src)
    findings = _check_file(f, tmp_path)
    assert findings == []


def test_unreachable_not_implemented_is_ignored(tmp_path):
    src = """
        def f(self):
            raise NotImplementedError("cannot reach")
    """
    f = _write(tmp_path, src)
    findings = _check_file(f, tmp_path)
    assert findings == []


def test_closure_size_validation_is_ignored(tmp_path):
    src = """
        def initialize_frame_scopes(self):
            if closure_size != nfreevars:
                raise ValueError(
                    "code object received a closure with "
                    "an unexpected number of free variables"
                )
    """
    f = _write(tmp_path, src)
    findings = _check_file(f, tmp_path)
    assert findings == []


def test_other_value_error_is_still_flagged(tmp_path):
    src = """
        def f(self):
            raise ValueError("some other error")
    """
    f = _write(tmp_path, src)
    findings = _check_file(f, tmp_path)
    assert len(findings) == 1
    assert findings[0]["type"] == "interp-app-boundary-raw-exception"


def test_count_callers_zero_for_unreferenced_function():
    source = "def fixedunpack(self, argcount):\n    raise ValueError('x')\n"
    assert _count_callers("fixedunpack", source) == 0


def test_count_callers_excludes_definition_itself():
    source = (
        "def fixedunpack(self, argcount):\n"
        "    raise ValueError('x')\n"
        "\n"
        "result = args.fixedunpack(1)\n"
    )
    assert _count_callers("fixedunpack", source) == 1


def test_count_callers_does_not_match_substring():
    # 'fixedunpack_extra(' should not count as a call to 'fixedunpack'.
    source = "def fixedunpack_extra():\n    pass\nfixedunpack_extra()\n"
    assert _count_callers("fixedunpack", source) == 0


# ---------------------------------------------------------------------------
# W_CLASS_NO_RELEASE tests (Issue #19 — Gap 2)
# ---------------------------------------------------------------------------

_IAB_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "interp_app"


def _run_iab_fixture(name: str):
    """Run analyze() on a single fixture file copied to a temp dir."""
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / name
        shutil.copy2(_IAB_FIXTURES / name, dest)
        return _analyze(tmp)


def test_w_class_open_no_close_flagged():
    """W_* class acquiring a resource without a release method is flagged."""
    result = _run_iab_fixture("w_class_open_no_close.py")
    subtypes = [f["subtype"] for f in result.get("findings", [])]
    assert "W_CLASS_NO_RELEASE" in subtypes, (
        f"Expected W_CLASS_NO_RELEASE in {subtypes}"
    )


def test_w_class_open_with_close_no_finding():
    """W_* class that has a descr_close method is NOT flagged."""
    result = _run_iab_fixture("w_class_open_with_close.py")
    nr_findings = [
        f for f in result.get("findings", [])
        if f.get("subtype") == "W_CLASS_NO_RELEASE"
    ]
    assert nr_findings == [], (
        f"Did not expect W_CLASS_NO_RELEASE for class with descr_close; got {nr_findings}"
    )
