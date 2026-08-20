"""Tests for scan_translated_divergence.py."""

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

from scan_translated_divergence import _classify_two_arm  # noqa: E402


def _body(source: str) -> list[ast.stmt]:
    return ast.parse(textwrap.dedent(source)).body


def test_translation_boundary_llop_is_not_fix():
    if_body = _body(
        """
        llop.debug_fatalerror(lltype.Void, msg)
        """
    )
    else_body = _body(
        """
        raise AssertGreenFailed(msg)
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_nontranslated_helper_is_not_fix():
    if_body = _body(
        """
        raise ContinueRunningNormally(args)
        """
    )
    else_body = _body(
        """
        self._nontranslated_run_directly(args, loop_token)
        assert 0
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_debug_print_traceback_is_not_fix():
    if_body = _body(
        """
        llop.debug_print_traceback(lltype.Void)
        """
    )
    else_body = _body(
        """
        raise
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_low_level_cast_is_not_fix():
    if_body = _body(
        """
        value = lltype.cast_ptr_to_int(gcref)
        return value
        """
    )
    else_body = _body(
        """
        return id(gcref._x)
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_gcref_representation_is_not_fix():
    if_body = _body(
        """
        return lltype.cast_opaque_ptr(llmemory.GCREF, x)
        """
    )
    else_body = _body(
        """
        return _GcRef(x)
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_raw_storage_operation_is_not_fix():
    if_body = _body(
        """
        return raw_storage_getitem(TP, storage, index)
        """
    )
    else_body = _body(
        """
        return _raw_storage_getitem_unchecked(TP, storage, index)
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_unrelated_different_calls_remain_consider():
    if_body = _body(
        """
        return translated_implementation(value)
        """
    )
    else_body = _body(
        """
        return python_implementation(value)
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "different substantive functions" in reason


def test_translation_boundary_untranslated_helper_is_not_fix():
    if_body = _body(
        """
        return llmemory.cast_adr_to_int(addr)
        """
    )
    else_body = _body(
        """
        return _start_of_page_untranslated(addr, page_size)
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_unrecognized_control_flow_difference_remains_fix():
    if_body = _body(
        """
        return value
        """
    )
    else_body = _body(
        """
        do_something()
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "FIX"
    assert reason == "arms have inconsistent return/raise control-flow shape"
