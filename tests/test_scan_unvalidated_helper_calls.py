"""Tests for scan_unvalidated_helper_calls.py.

Includes a regression fixture for the real bug found while validating this
scanner against the actual checkout: a call inside a nested function was
being processed once per enclosing function (including outer ones), so a
guard pattern belonging to a sibling nested function could wrongly clear an
unguarded call in a different nested function. Confirmed on real code:
pypy/objspace/std/newformat.py's nested _lit() function.
"""

import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "pypy-review-toolkit" / "scripts"))

from scan_unvalidated_helper_calls import _check_file  # noqa: E402

_HELPERS = [
    {
        "qualname": "rutf8.unichr_as_utf8",
        "precondition": "must be <= 0x10FFFF",
        "guard_pattern_hints": ["0x10FFFF", "0x110000", "1114111"],
    }
]


def _write(tmp_path: Path, source: str) -> Path:
    f = tmp_path / "sample.py"
    f.write_text(textwrap.dedent(source))
    return f


def test_unguarded_call_is_flagged_consider(tmp_path):
    src = """
        def append_utf8(self, value):
            w_ch = self.space.newutf8(rutf8.unichr_as_utf8(r_uint(value)), 1)
    """
    f = _write(tmp_path, src)
    findings = _check_file(f, tmp_path, _HELPERS)
    assert len(findings) == 1
    assert findings[0]["type"] == "unvalidated-helper-call"
    assert findings[0]["classification"] == "CONSIDER"


def test_guarded_call_is_flagged_acceptable(tmp_path):
    src = """
        def unichr(space, code):
            if code < 0 or code > 0x10FFFF:
                raise oefmt(space.w_ValueError, "unichr() arg out of range")
            s = rutf8.unichr_as_utf8(code, allow_surrogates=True)
            return space.newutf8(s, 1)
    """
    f = _write(tmp_path, src)
    findings = _check_file(f, tmp_path, _HELPERS)
    assert len(findings) == 1
    assert findings[0]["type"] == "unvalidated-helper-call-guarded"
    assert findings[0]["classification"] == "ACCEPTABLE"


def test_nested_function_guard_does_not_leak_to_sibling(tmp_path):
    # Regression fixture: a guard in one nested function must NOT clear an
    # unguarded call in a different nested function of the same outer
    # function -- both nested functions get walked when the outer function
    # is processed, and an earlier version of this scanner scoped calls to
    # every enclosing function, not just the innermost one.
    src = """
        class Foo(object):
            def outer(self):
                def _guarded(code):
                    if code < 0 or code > 0x10FFFF:
                        raise ValueError("bad")
                    return rutf8.unichr_as_utf8(code)

                def _unguarded(s):
                    return rutf8.unichr_as_utf8(ord(s[0]))

                return _guarded, _unguarded
    """
    f = _write(tmp_path, src)
    findings = _check_file(f, tmp_path, _HELPERS)
    assert len(findings) == 2
    by_type = {f["type"] for f in findings}
    assert by_type == {"unvalidated-helper-call", "unvalidated-helper-call-guarded"}
    # Exactly one of each -- not two of the same type, which is what the
    # bug produced (the unguarded call got double-counted: once correctly
    # as unguarded via its own scope, once incorrectly as guarded via the
    # outer function's broader scope, which contained _guarded's check text).
    guarded = [f for f in findings if f["type"] == "unvalidated-helper-call-guarded"]
    unguarded = [f for f in findings if f["type"] == "unvalidated-helper-call"]
    assert len(guarded) == 1
    assert len(unguarded) == 1


def test_call_with_no_matching_helper_not_flagged(tmp_path):
    src = """
        def f():
            return some_other_function(1, 2, 3)
    """
    f = _write(tmp_path, src)
    findings = _check_file(f, tmp_path, _HELPERS)
    assert findings == []


def test_bare_imported_name_form_also_matches(tmp_path):
    # from rpython.rlib.rutf8 import unichr_as_utf8 -- bare name, not dotted.
    src = """
        def f(code):
            return unichr_as_utf8(code)
    """
    f = _write(tmp_path, src)
    findings = _check_file(f, tmp_path, _HELPERS)
    assert len(findings) == 1
    assert findings[0]["type"] == "unvalidated-helper-call"


# ---------------------------------------------------------------------------
# Multi-clause precondition tests.
#
# The motivating defect: `rutf8.unichr_as_utf8`'s contract has two independent
# clauses -- "<= 0x10FFFF" AND "not a surrogate unless allow_surrogates=True".
# A single hint list conflates them, so a range check clears the call and the
# surrogate obligation goes unchecked. That is a live bug on real code:
# pypy/objspace/std/newformat.py:847 range-checks three lines above the call
# and omits allow_surrogates, so `format(0xD800, 'c')` leaks SystemError on
# PyPy 7.3.23 while CPython returns the surrogate string.
# ---------------------------------------------------------------------------

_CLAUSED_HELPERS = [
    {
        "qualname": "rutf8.unichr_as_utf8",
        "precondition": "<= 0x10FFFF and not a surrogate unless allow_surrogates=True",
        "guard_pattern_hints": ["0x10FFFF", "0x110000", "1114111"],
        "exception_guard_hints": ["OutOfRange"],
        "clauses": [
            {
                "name": "range",
                "text_hints": ["0x10FFFF", "0x110000", "1114111"],
                "provenance_hints": ["codepoint_at_pos", "codepoints_in_utf8"],
            },
            {
                "name": "surrogate",
                "discharged_by_kwarg": "allow_surrogates",
                "text_hints": ["0xD800", "0xDfff", "0xDFFF"],
            },
        ],
    }
]

_BIGINT_HELPERS = [
    {
        "qualname": "rbigint.tobytes",
        "precondition": "signed must match the value's sign",
        "guard_pattern_hints": ["< 0", ">= 0", ".sign"],
        "exception_guard_hints": [
            "InvalidSignednessError",
            "InvalidEndiannessError",
            "OverflowError",
        ],
    }
]


def test_range_check_alone_does_not_clear_the_surrogate_clause(tmp_path):
    """The newformat.py:847 shape -- confirmed SIGSEGV-adjacent leak on PyPy."""
    src = """
        def fmt_c(self, space, w_num):
            value = space.int_w(w_num)
            if not (0 <= value <= 0x10FFFF):
                raise oefmt(space.w_OverflowError, "out of range")
            result = rutf8.unichr_as_utf8(value)
    """
    findings = _check_file(_write(tmp_path, src), tmp_path, _CLAUSED_HELPERS)
    assert len(findings) == 1
    assert findings[0]["classification"] == "CONSIDER"
    assert "surrogate" in findings[0]["message"]
    assert "range" not in findings[0]["message"].split("clause(s)")[0]


def test_allow_surrogates_kwarg_discharges_the_surrogate_clause(tmp_path):
    """The formatting.py:516 shape."""
    src = """
        def fmt_c(self, space, n):
            if not (0 <= n <= 0x10FFFF):
                raise oefmt(space.w_OverflowError, "out of range")
            c = rutf8.unichr_as_utf8(r_uint(n), allow_surrogates=True)
    """
    findings = _check_file(_write(tmp_path, src), tmp_path, _CLAUSED_HELPERS)
    assert findings[0]["classification"] == "ACCEPTABLE"


def test_non_literal_kwarg_is_not_treated_as_discharged(tmp_path):
    src = """
        def fmt_c(self, space, n, allow):
            if not (0 <= n <= 0x10FFFF):
                raise oefmt(space.w_OverflowError, "out of range")
            c = rutf8.unichr_as_utf8(r_uint(n), allow_surrogates=allow)
    """
    findings = _check_file(_write(tmp_path, src), tmp_path, _CLAUSED_HELPERS)
    assert findings[0]["classification"] == "CONSIDER"


def test_reactive_except_handler_discharges_all_clauses(tmp_path):
    """The unicodeobject.py:2420 shape -- PyPy's reactive idiom, a real guard."""
    src = """
        def descr_numeric(self, space, uchr):
            try:
                c = rutf8.unichr_as_utf8(r_uint(uchr))
            except rutf8.OutOfRange:
                raise oefmt(space.w_UnicodeError, "surrogates not allowed")
    """
    findings = _check_file(_write(tmp_path, src), tmp_path, _CLAUSED_HELPERS)
    assert findings[0]["classification"] == "ACCEPTABLE"


def test_except_handler_does_not_protect_calls_inside_the_handler(tmp_path):
    src = """
        def descr_numeric(self, space, uchr):
            try:
                pass
            except rutf8.OutOfRange:
                c = rutf8.unichr_as_utf8(r_uint(uchr))
    """
    findings = _check_file(_write(tmp_path, src), tmp_path, _CLAUSED_HELPERS)
    assert findings[0]["classification"] == "CONSIDER"


def test_validated_utf8_provenance_discharges_the_range_clause(tmp_path):
    """The formatting.py:351 shape -- a codepoint read out of a valid utf8 str."""
    src = """
        def _get_error_info(self, pos):
            cp = rutf8.codepoint_at_pos(self.fmt, pos)
            w_s = space.newutf8(rutf8.unichr_as_utf8(r_uint(cp),
                                                     allow_surrogates=True), 1)
    """
    findings = _check_file(_write(tmp_path, src), tmp_path, _CLAUSED_HELPERS)
    assert findings[0]["classification"] == "ACCEPTABLE"


def test_partial_exception_handling_stays_consider(tmp_path):
    """Confirmed bug #3: setstate catches OverflowError but not the signedness error."""
    src = """
        def setstate_w(self, space, w_state):
            bigint = space.bigint_w(w_state)
            try:
                statebytes = bigint.tobytes(17, 'little', False)
            except OverflowError:
                raise oefmt(space.w_UnicodeError, "pending buffer too large")
    """
    findings = _check_file(_write(tmp_path, src), tmp_path, _BIGINT_HELPERS)
    assert len(findings) == 1
    assert findings[0]["classification"] == "CONSIDER"


def test_complete_exception_handling_is_acceptable(tmp_path):
    """int.to_bytes catches all three -- the correctly-guarded twin of bug #3."""
    src = """
        def descr_to_bytes(self, space, length, byteorder, signed):
            bigint = space.bigint_w(self)
            try:
                byte_string = bigint.tobytes(length, byteorder=byteorder, signed=signed)
            except InvalidEndiannessError:
                raise oefmt(space.w_ValueError, "byteorder must be 'little' or 'big'")
            except InvalidSignednessError:
                raise oefmt(space.w_OverflowError, "can't convert negative int")
            except OverflowError:
                raise oefmt(space.w_OverflowError, "int too big to convert")
    """
    findings = _check_file(_write(tmp_path, src), tmp_path, _BIGINT_HELPERS)
    assert findings[0]["classification"] == "ACCEPTABLE"


def test_helpers_without_clauses_keep_single_clause_behaviour(tmp_path):
    """Backwards compatibility: an entry with no `clauses` key is unchanged."""
    src = """
        def fmt_c(self, space, w_num):
            value = space.int_w(w_num)
            if not (0 <= value <= 0x10FFFF):
                raise oefmt(space.w_OverflowError, "out of range")
            result = rutf8.unichr_as_utf8(value)
    """
    findings = _check_file(_write(tmp_path, src), tmp_path, _HELPERS)
    assert findings[0]["classification"] == "ACCEPTABLE"
