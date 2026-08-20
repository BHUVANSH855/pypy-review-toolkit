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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "pypy-review-toolkit" / "scripts"))

from discover_pypy import _pypy_version_string  # noqa: E402


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
