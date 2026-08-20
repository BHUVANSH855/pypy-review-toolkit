"""Tests for scan_immutability_contracts.py."""

import ast
import sys
import textwrap
from pathlib import Path

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parent.parent
        / "plugins"
        / "pypy-review-toolkit"
        / "scripts"
    ),
)

import scan_immutability_contracts as scanner  # noqa: E402
from scan_immutability_contracts import (
    _is_guarded_helper_lazy_init,
    _is_lazy_initialized_field,
)


def _class(source: str) -> ast.ClassDef:
    tree = ast.parse(textwrap.dedent(source))
    return tree.body[0]


def test_null_initialized_field_with_guard_is_lazy_init():
    node = _class(
        """
        class CPPMethod:
            def __init__(self):
                self.cif_descr = None

            def _setup(self):
                if self.cif_descr is None:
                    self.cif_descr = build()
        """
    )

    assert _is_lazy_initialized_field(node, "cif_descr", ["_setup"])


def test_nullptr_initialized_field_with_guard_is_lazy_init():
    node = _class(
        """
        class CPPMethod:
            def __init__(self):
                self.cif_descr = lltype.nullptr(CIF_DESCRIPTION)

            def _setup(self):
                if self.cif_descr == lltype.nullptr(CIF_DESCRIPTION):
                    self.cif_descr = build()
        """
    )

    assert _is_lazy_initialized_field(node, "cif_descr", ["_setup"])


def test_unrelated_mutation_is_not_lazy_init():
    node = _class(
        """
        class Example:
            def __init__(self):
                self.value = None

            def update(self):
                self.value = compute()
        """
    )

    assert not _is_lazy_initialized_field(node, "value", ["update"])


def test_non_null_initialization_is_not_lazy_init():
    node = _class(
        """
        class Example:
            def __init__(self):
                self.value = initial()

            def update(self):
                if self.value is None:
                    self.value = compute()
        """
    )

    assert not _is_lazy_initialized_field(node, "value", ["update"])


def test_descr_init_assignment_is_not_reported(tmp_path):
    source = """
        class W_Struct(object):
            _immutable_fields_ = ["format"]

            def descr__init__(self, format):
                self.format = format
    """
    path = tmp_path / "test.py"
    path.write_text(textwrap.dedent(source))

    findings = scanner._check_file(path, tmp_path)

    assert findings == []


def test_init_helper_assignment_is_not_reported(tmp_path):
    source = """
        class UnboxedPlainAttribute(object):
            _immutable_fields_ = ["listindex"]

            def __init__(self):
                self._compute_listindex()

            def _compute_listindex(self):
                self.listindex = 0
    """
    path = tmp_path / "test.py"
    path.write_text(textwrap.dedent(source))

    findings = scanner._check_file(path, tmp_path)

    assert findings == []


def test_guarded_helper_lazy_init_is_detected():
    node = _class(
        """
        class CPPMethod:
            def __init__(self):
                self.executor = None
                self.converters = None

            def _setup(self):
                self.executor = build_executor()

            def call(self):
                if self.converters is None:
                    self._setup()
        """
    )

    assert _is_lazy_initialized_field(
        node,
        "executor",
        ["_setup"],
    )


def test_state_restoration_assignment_is_not_reported(tmp_path):
    source = """
        class GeneratorIterator(object):
            _immutable_fields_ = ["pycode"]

            def __init__(self):
                self.pycode = initial()

            def descr__setstate__(self):
                self.pycode = None
    """
    path = tmp_path / "test.py"
    path.write_text(textwrap.dedent(source))

    findings = scanner._check_file(path, tmp_path)

    assert findings == []


def test_translation_cleanup_assignment_is_not_reported(tmp_path):
    source = """
        class PyCode(object):
            _immutable_fields_ = ["co_filename"]

            def __init__(self):
                self.co_filename = filename

            def _cleanup_(self):
                self.co_filename = freeze_filename()
    """
    path = tmp_path / "test.py"
    path.write_text(textwrap.dedent(source))

    findings = scanner._check_file(path, tmp_path)

    assert findings == []


def test_class_level_null_lazy_init_is_detected():
    node = _class(
        """
        class W_RawFuncType:
            _immutable_fields_ = ["nostruct_ctype"]
            nostruct_ctype = None

            def prepare(self):
                if self.nostruct_ctype is None:
                    self.nostruct_ctype = build()
        """
    )

    assert _is_lazy_initialized_field(
        node,
        "nostruct_ctype",
        ["prepare"],
    )


def test_finalizer_assignment_is_not_reported(tmp_path):
    source = """
        class W_StructInstance(object):
            _immutable_fields_ = ["rawmem"]

            def __init__(self):
                self.rawmem = allocate()

            def __del__(self):
                if self.rawmem:
                    free(self.rawmem)
                    self.rawmem = None
    """
    path = tmp_path / "test.py"
    path.write_text(textwrap.dedent(source))

    findings = scanner._check_file(path, tmp_path)

    assert findings == []


def test_multiple_fields_initialized_in_same_guard_are_lazy_init():
    node = _class(
        """
        class W_RawFuncType:
            _immutable_fields_ = [
                "nostruct_ctype",
                "nostruct_locs",
                "nostruct_nargs",
            ]

            nostruct_ctype = None
            nostruct_locs = None
            nostruct_nargs = 0

            def prepare(self):
                if self.nostruct_ctype is None:
                    self.nostruct_ctype = build_type()
                    self.nostruct_locs = build_locs()
                    self.nostruct_nargs = build_nargs()
        """
    )

    assert _is_lazy_initialized_field(
        node,
        "nostruct_ctype",
        ["prepare"],
    )
    assert _is_lazy_initialized_field(
        node,
        "nostruct_locs",
        ["prepare"],
    )
    assert _is_guarded_helper_lazy_init(
        node,
        "nostruct_nargs",
    )


def test_non_null_field_in_guarded_lazy_init_group_is_detected():
    node = _class(
        """
        class W_RawFuncType:
            _immutable_fields_ = [
                "nostruct_ctype",
                "nostruct_locs",
                "nostruct_nargs",
            ]

            nostruct_ctype = None
            nostruct_locs = None
            nostruct_nargs = 0

            def prepare(self):
                if self.nostruct_ctype is None:
                    self.nostruct_ctype = build_type()
                    self.nostruct_locs = build_locs()
                    self.nostruct_nargs = build_nargs()
        """
    )

    assert _is_lazy_initialized_field(
        node,
        "nostruct_nargs",
        ["prepare"],
    )
