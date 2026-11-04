"""Tests for discover_pypy.py's checkout-metadata detection.

The version-hint regression these cover is the reason the file exists: design
doc §9.4 added ``pypy_cpython_version_hint`` specifically so a report says which
PyPy line it was actually run against, after §0.2 found that every earlier
census had been run against ``main`` (Python 2.7.18) while the confirmed bugs
lived on the 3.11 line. The field read ``pypy/tool/version.py``, which does not
exist in any PyPy checkout, so it returned ``None`` unconditionally and the
safeguard never fired.
"""

import sys
import types
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "pypy-review-toolkit" / "scripts"))

from discover_pypy import _pypy_version_string, _slice_priority, build_review_slices, IN_SCOPE_LAYERS  # noqa: E402


def _make_checkout(tmp_path: Path, body: str) -> Path:
    version_file = tmp_path / "pypy" / "module" / "sys" / "version.py"
    version_file.parent.mkdir(parents=True)
    version_file.write_text(body, encoding="utf-8")
    return tmp_path


def test_version_hint_reads_module_sys_version(tmp_path):
    """The real path is pypy/module/sys/version.py, not pypy/tool/version.py."""
    root = _make_checkout(
        tmp_path,
        'CPYTHON_VERSION            = (3, 11, 15, "final", 0)\n'
        'PYPY_VERSION               = (8, 0, 0, "alpha", 0)\n',
    )
    assert _pypy_version_string(root) == '3,11,15,"final",0 [PyPy 8,0,0,"alpha",0]'


def test_version_hint_distinguishes_the_2_7_line(tmp_path):
    """The branch-mismatch case §0.2 describes must be visible in the output."""
    root = _make_checkout(
        tmp_path,
        'CPYTHON_VERSION            = (2, 7, 18, "final", 0)\n'
        'PYPY_VERSION               = (8, 0, 0, "alpha", 0)\n',
    )
    hint = _pypy_version_string(root)
    assert hint is not None and hint.startswith("2,7,18")


def test_version_hint_without_pypy_version(tmp_path):
    root = _make_checkout(tmp_path, 'CPYTHON_VERSION = (3, 11, 15, "final", 0)\n')
    assert _pypy_version_string(root) == '3,11,15,"final",0'


def test_version_hint_ignores_non_toplevel_assignment(tmp_path):
    """A mention inside a function body is not the module-level declaration."""
    root = _make_checkout(
        tmp_path,
        "def _make_version_template(CPYTHON_VERSION=None):\n"
        '    CPYTHON_VERSION = (9, 9, 9, "bogus", 0)\n',
    )
    assert _pypy_version_string(root) is None


def test_version_hint_missing_file(tmp_path):
    assert _pypy_version_string(tmp_path) is None


# ---------------------------------------------------------------------------
# _slice_priority tests
# ---------------------------------------------------------------------------

def test_slice_priority_critical_for_io_files():
    """Files under pypy/module/_io get the critical tier."""
    files = ["pypy/module/_io/interp_textio.py", "pypy/module/_io/interp_io.py"]
    assert _slice_priority("module", files) == "critical"


def test_slice_priority_high_for_rlib_layer():
    """The rlib layer (confirmed bugs) gets the high tier."""
    files = ["rpython/rlib/rsocket.py", "rpython/rlib/rstring.py"]
    assert _slice_priority("rlib", files) == "high"


def test_slice_priority_high_for_lib_pypy_layer():
    """The lib_pypy layer (CFFI bugs) gets the high tier."""
    files = ["lib_pypy/_lzma.py", "lib_pypy/_sqlite3.py"]
    assert _slice_priority("lib_pypy", files) == "high"


def test_slice_priority_high_for_module_layer_non_io():
    """The module layer (without _io files) also gets the high tier."""
    files = ["pypy/module/posix/interp_posix.py"]
    assert _slice_priority("module", files) == "high"


def test_slice_priority_normal_for_jit_layer():
    """The jit layer (no confirmed bugs yet) gets the normal tier."""
    files = ["rpython/jit/metainterp/compile.py"]
    assert _slice_priority("jit", files) == "normal"


def test_slice_priority_critical_wins_over_high():
    """A slice mixing _io files with rlib files should still be critical."""
    files = ["pypy/module/_io/interp_io.py", "pypy/module/sys/vm.py"]
    assert _slice_priority("module", files) == "critical"


# ---------------------------------------------------------------------------
# build_review_slices — review_priority field
# ---------------------------------------------------------------------------

def _make_pypy_tree(tmp_path: Path) -> tuple[Path, dict]:
    """Build a minimal synthetic PyPy tree; return (root, files_by_layer)."""
    files_by_layer: dict[str, list[str]] = {
        "rlib": ["rpython/rlib/rsocket.py"],
        "jit": ["rpython/jit/metainterp/compile.py"],
        "gc": ["rpython/memory/gc/incminimark.py"],
        "interpreter": ["pypy/interpreter/pyframe.py"],
        "objspace": ["pypy/objspace/std/bytesobject.py"],
        "module": ["pypy/module/posix/interp_posix.py", "pypy/module/_io/interp_io.py"],
        "lib_pypy": ["lib_pypy/_lzma.py"],
    }
    for rel_paths in files_by_layer.values():
        for rel in rel_paths:
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("# synthetic\n" * 100, encoding="utf-8")
    return tmp_path, files_by_layer


def test_build_review_slices_all_have_review_priority(tmp_path):
    """Every slice produced by build_review_slices() must have review_priority."""
    root, files_by_layer = _make_pypy_tree(tmp_path)
    discovery = {"checkout_root": str(root), "files_by_layer": files_by_layer}
    result = build_review_slices(discovery)
    slices = result.get("slices", [])
    assert slices, "Expected at least one slice"
    for s in slices:
        assert "review_priority" in s, f"Slice missing review_priority: {s}"
        assert s["review_priority"] in {"critical", "high", "normal"}


def test_build_review_slices_io_files_are_critical(tmp_path):
    """Slices containing pypy/module/_io files must have review_priority=critical."""
    root, files_by_layer = _make_pypy_tree(tmp_path)
    discovery = {"checkout_root": str(root), "files_by_layer": files_by_layer}
    result = build_review_slices(discovery)
    critical_slices = [
        s for s in result["slices"]
        if any(f.startswith("pypy/module/_io") for f in s["files"])
    ]
    assert critical_slices, "Expected at least one critical slice from _io files"
    for s in critical_slices:
        assert s["review_priority"] == "critical"


def test_build_review_slices_rlib_layer_is_high(tmp_path):
    """Slices in the rlib layer must have review_priority=high."""
    root, files_by_layer = _make_pypy_tree(tmp_path)
    discovery = {"checkout_root": str(root), "files_by_layer": files_by_layer}
    result = build_review_slices(discovery)
    rlib_slices = [s for s in result["slices"] if s["layer"] == "rlib"]
    assert rlib_slices, "Expected at least one rlib slice"
    for s in rlib_slices:
        assert s["review_priority"] == "high"


# ---------------------------------------------------------------------------
# git_commit_date in discover() return value
# ---------------------------------------------------------------------------

def test_discover_includes_git_commit_date(tmp_path):
    """discover() must include git_commit_date in its return dict."""
    from discover_pypy import discover

    # Build a minimal recognizable checkout
    (tmp_path / "rpython" / "jit").mkdir(parents=True)
    (tmp_path / "pypy" / "interpreter").mkdir(parents=True)

    fake_date = "2024-11-15 10:30:00 +0100"

    def fake_run(cmd, **kwargs):
        result = MagicMock()
        if "--short" in cmd:
            result.returncode = 0
            result.stdout = "fe2af584\n"
        elif "--format=%ci" in cmd:
            result.returncode = 0
            result.stdout = fake_date + "\n"
        elif "rev-parse" in cmd and "--abbrev-ref" in cmd:
            result.returncode = 0
            result.stdout = "py3.11\n"
        else:
            result.returncode = 1
            result.stdout = ""
        return result

    with patch("discover_pypy.subprocess.run", side_effect=fake_run):
        result = discover(str(tmp_path))

    assert "git_commit_date" in result
    assert result["git_commit_date"] == fake_date
