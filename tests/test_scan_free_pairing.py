"""Tests for scan_free_pairing.py."""

import sys
import textwrap
from pathlib import Path

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
