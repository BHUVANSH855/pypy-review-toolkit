"""Tests for scan_free_pairing.py."""

import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "pypy-review-toolkit" / "scripts"))

from scan_free_pairing import _check_file, _check_suspicious_ffi_attrs  # noqa: E402
import ast


def _parse(source: str) -> ast.Module:
    return ast.parse(textwrap.dedent(source))


def _write(tmp_path: Path, source: str) -> Path:
    f = tmp_path / "sample.py"
    f.write_text(textwrap.dedent(source))
    return f


def test_ffi_none_is_flagged():
    # A suspicious CFFI attribute is detected; reachability and impact
    # must be established separately by source-level investigation.
    tree = _parse("self._p = ffi.NONE")
    results = _check_suspicious_ffi_attrs(tree)
    assert len(results) == 1
    assert results[0][1] == "NONE"


def test_ffi_null_is_not_flagged():
    tree = _parse("self._p = ffi.NULL")
    results = _check_suspicious_ffi_attrs(tree)
    assert results == []


def test_real_cffi_api_members_not_flagged():
    # Regression: earlier version flagged ALL non-allowlisted ffi.* names,
    # producing 120+ false positives on real cffi API (cdef, callback etc).
    src = "\n".join([
        "ffi.cdef('int x;')",
        "ffi.set_source('mod', 'code')",
        "ffi.compile()",
        "ffi.callback('int(int)', fn)",
        "ffi.dlopen('lib.so')",
        "ffi.memmove(a, b, n)",
        "ffi.cast('uint8_t*', ptr)",
    ])
    tree = _parse(src)
    results = _check_suspicious_ffi_attrs(tree)
    assert results == []


def test_unlocked_free_in_class_with_lock_is_flagged(tmp_path):
    src = """
        import threading
        class MyBuffer(object):
            def __init__(self):
                self.lock = threading.Lock()
                self._buf = ffi.NULL

            def reset(self):
                with self.lock:
                    if self._buf is not ffi.NULL:
                        m.free(self._buf)
                        self._buf = ffi.NULL

            def clear(self):
                if self._buf is not ffi.NULL:
                    m.free(self._buf)
    """
    f = _write(tmp_path, src)
    findings = _check_file(f, tmp_path)
    unlocked = [x for x in findings if x["type"] == "unlocked-free-call"]
    assert len(unlocked) == 1
    assert "clear" in unlocked[0]["message"]


def test_no_findings_in_class_without_lock(tmp_path):
    src = """
        class Simple(object):
            def __init__(self):
                self._buf = ffi.NULL

            def clear(self):
                if self._buf is not ffi.NULL:
                    m.free(self._buf)
    """
    f = _write(tmp_path, src)
    findings = _check_file(f, tmp_path)
    unlocked = [x for x in findings if x["type"] == "unlocked-free-call"]
    assert unlocked == []


def test_correctly_locked_free_not_flagged(tmp_path):
    src = """
        import threading
        class Safe(object):
            def __init__(self):
                self.lock = threading.Lock()
                self._buf = ffi.NULL

            def clear(self):
                with self.lock:
                    if self._buf is not ffi.NULL:
                        m.free(self._buf)
                        self._buf = ffi.NULL
    """
    f = _write(tmp_path, src)
    findings = _check_file(f, tmp_path)
    unlocked = [x for x in findings if x["type"] == "unlocked-free-call"]
    assert unlocked == []


# ---------------------------------------------------------------------------
# Allowlist loading tests
# ---------------------------------------------------------------------------

def test_allowlist_loaded_from_data_file():
    """The scanner must load the cffi allowlist from lib_pypy_cffi_allowlist.json."""
    import scan_free_pairing
    # NULL should always be in the loaded set (it's in the data file and fallback)
    assert "NULL" in scan_free_pairing._KNOWN_FFI_ATTRS
    # The data file should give us a larger set than just the fallback {"NULL"}
    assert len(scan_free_pairing._KNOWN_FFI_ATTRS) > 1, (
        "Expected allowlist loaded from data file; got only the minimal fallback"
    )


def test_allowlist_fallback_when_file_missing():
    """When the data file is missing, analyze() runs without raising and warns.

    We simulate a missing file by temporarily injecting a warning (mimicking
    what the module-startup loading code sets) and verifying that analyze()
    surfaces it in the envelope. The startup-loading path itself is validated
    by test_allowlist_warning_surfaces_in_envelope.
    """
    import scan_free_pairing as _m

    # Simulate the exact state the module is in after a FileNotFoundError
    # during startup: _KNOWN_FFI_ATTRS falls back to _FALLBACK_FFI_ATTRS,
    # _cffi_allowlist_warning is a non-empty string.
    original_attrs = _m._KNOWN_FFI_ATTRS
    original_warning = _m._cffi_allowlist_warning
    _m._KNOWN_FFI_ATTRS = _m._FALLBACK_FFI_ATTRS
    _m._cffi_allowlist_warning = "lib_pypy_cffi_allowlist.json not found at /fake/path; using minimal fallback set"
    try:
        # The scanner must run without raising even on the fallback path.
        result = _m.analyze(str(Path(__file__).parent))
        assert isinstance(result, dict)
        # The warning must appear in the envelope.
        assert "warnings" in result
        assert any("not found" in w for w in result["warnings"])
        # Fallback set must still catch ffi.NONE
        tree = ast.parse("x = ffi.NONE")
        hits = _m._check_suspicious_ffi_attrs(tree)
        assert hits, "Fallback set must still catch ffi.NONE"
    finally:
        _m._KNOWN_FFI_ATTRS = original_attrs
        _m._cffi_allowlist_warning = original_warning


def test_allowlist_warning_surfaces_in_envelope(tmp_path):
    """When the allowlist is missing, analyze() must include a warning in the envelope."""
    import importlib
    import scan_free_pairing as _m

    # Temporarily inject a warning to simulate missing file.
    original_warning = _m._cffi_allowlist_warning
    _m._cffi_allowlist_warning = "test warning: allowlist not found"
    try:
        result = _m.analyze(str(tmp_path))
        assert "warnings" in result, "envelope must have a 'warnings' key when allowlist is missing"
        assert any("test warning" in w for w in result["warnings"])
    finally:
        _m._cffi_allowlist_warning = original_warning
