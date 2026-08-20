#!/usr/bin/env python3
"""Detect unvalidated calls into RPython helpers with unstated preconditions.

**Highest-priority scanner in this toolkit, per design doc §9.1.** Two of
danzin's five fusil-confirmed real bugs (`docs/PYPY_FINDINGS_REPORT.md`, #2
and #3) match this exact shape: a value that came straight from app-level
input is passed into an RPython helper without the range/sign check that
helper's contract requires, and the helper leaks PyPy's generic
``SystemError: unexpected internal exception (please report a bug)`` instead
of the proper app-level exception CPython raises for the same input.

**Confirmed real, not hypothetical**: both helpers currently tracked in
``data/sensitive_rpython_helpers.json`` were verified to exist exactly as
named in the checkout (``rpython/rlib/rutf8.py:40``,
``rpython/rlib/rbigint.py:392``), and the exact buggy call site for bug #2
was located and confirmed unguarded: ``pypy/module/struct/formatiterator.py``'s
``append_utf8`` calls ``rutf8.unichr_as_utf8(r_uint(value))`` with no
preceding check at all. Two correctly-guarded call sites of the same helper
were also found and confirmed to have a real, matching guard
(``pypy/module/__builtin__/operation.py``'s ``unichr``:
``if code < 0 or code > 0x10FFFF: raise ...``;
``pypy/module/_codecs/interp_codecs.py``'s charmap decoder:
``if not 0 <= x <= 0x10FFFF: raise ...``) -- confirming this scanner's guard
heuristic can actually distinguish the two.

**v0.1 approach: curated list, not general taint analysis.** Real data-flow
tracing (following an app-level value through arbitrary intermediate
variables to a helper call) is out of scope for this version. Instead: flag
any call to a listed helper, in any function, where no comparison against a
plausible guard constant (from the helper's ``guard_pattern_hints``) appears
anywhere earlier in the same function. This will under-report (a real check
via an intermediate variable or helper function won't be seen) and the list
starts at 3 entries. Growing the list from future confirmed bugs is the main
way this scanner becomes more valuable over time.

Usage:
    python scan_unvalidated_helper_calls.py [path] [--max-files N]
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import (  # noqa: E402
    collect_python_files,
    deduplicate_findings,
    emit,
    find_project_root,
    parse_common_args,
    relative_to_root,
)
from pypy_utils import parse_rpython_file  # noqa: E402
from discover_pypy import build_pypy_envelope, discover  # noqa: E402

_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "sensitive_rpython_helpers.json"


def _load_helpers() -> list[dict]:
    with _DATA_PATH.open() as f:
        return json.load(f)["helpers"]


def _finding(
    ftype: str,
    classification: str,
    confidence: str,
    node: ast.AST,
    message: str,
    detail: str = "",
) -> dict:
    return {
        "type": ftype,
        "classification": classification,
        "confidence": confidence,
        "line": getattr(node, "lineno", 0),
        "column": getattr(node, "col_offset", 0),
        "message": message,
        "detail": detail,
    }


def _call_matches_helper(call: ast.Call, qualname: str) -> bool:
    """True if *call* looks like a call to *qualname* (e.g. 'rutf8.unichr_as_utf8').

    Matches both the dotted form (``rutf8.unichr_as_utf8(...)``) and the
    bare imported-name form (``unichr_as_utf8(...)`` after ``from
    rpython.rlib.rutf8 import unichr_as_utf8``, confirmed as a real import
    style in ``pypy/module/unicodedata/interp_ucd.py``).
    """
    short_name = qualname.rsplit(".", 1)[-1]
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr == short_name
    if isinstance(func, ast.Name):
        return func.id == short_name
    return False


def _function_source_lines(func_node: ast.FunctionDef, source_lines: list[str]) -> str:
    start = func_node.lineno - 1
    end = getattr(func_node, "end_lineno", func_node.lineno)
    return "\n".join(source_lines[start:end])


def _has_guard_hint(func_text: str, hints: list[str]) -> bool:
    return any(hint in func_text for hint in hints)


def _call_has_truthy_kwarg(call: ast.Call, name: str) -> bool:
    """True if *call* passes ``name=True`` explicitly.

    This is the one signal that actually predicts safety for
    ``rutf8.unichr_as_utf8``: across all 24 call sites in ``pypy/``, presence of
    ``allow_surrogates=True`` separates the safe calls from the unsafe ones
    better than any numeric range check, because it discharges the *second*
    clause of the precondition rather than the first.
    """
    for kw in call.keywords:
        if kw.arg != name:
            continue
        value = kw.value
        if isinstance(value, ast.Constant) and value.value is True:
            return True
        # A non-literal (a variable, an `or`, a call) is not evidence either
        # way -- treat it as undischarged rather than guessing.
    return False


def _guarded_by_except(call: ast.AST, parent: dict, exc_hints: list[str]) -> bool:
    """True if EVERY exception in *exc_hints* is handled around *call*.

    PyPy's actual idiom for these helpers is frequently *reactive* -- let the
    RPython exception fly and convert it -- rather than the *proactive* numeric
    check ``guard_pattern_hints`` models. ``unicodeobject.py:2420``'s
    ``except rutf8.OutOfRange`` is a real, correct guard that a purely numeric
    hint list scores as unguarded.

    **All** listed exceptions must be handled, not merely one. That requirement
    is what separates the two real ``rbigint.tobytes`` call sites:

    - ``pypy/objspace/std/intobject.py:131`` (``int.to_bytes``) catches all of
      ``InvalidEndiannessError``, ``InvalidSignednessError`` and
      ``OverflowError`` -- genuinely guarded.
    - ``pypy/module/_multibytecodec/interp_incremental.py:170`` (``setstate``)
      catches ``OverflowError`` **only**, so a negative state lets
      ``InvalidSignednessError`` escape as ``SystemError``. That is confirmed
      bug #3. An any-of rule scores it identically to ``int.to_bytes`` and
      clears it.

    Only the ``try:`` body counts. A call inside an ``except`` handler is not
    protected by that handler. Handlers are collected across every enclosing
    ``try``, so a split guard still counts.
    """
    if not exc_hints:
        return False
    handled: set[str] = set()
    child: ast.AST = call
    cur: ast.AST = call
    while cur in parent:
        cur = parent[cur]
        if isinstance(cur, ast.Try) and any(child is stmt for stmt in cur.body):
            for handler in cur.handlers:
                if handler.type is None:
                    continue
                handler_text = ast.dump(handler.type)
                for hint in exc_hints:
                    if hint in handler_text:
                        handled.add(hint)
        child = cur
    return handled.issuperset(exc_hints)


def _undischarged_clauses(
    call: ast.Call,
    func_text: str,
    helper: dict,
    parent: dict,
) -> list[str]:
    """Names of precondition clauses this call site does not discharge.

    A helper whose precondition has more than one independent clause needs each
    checked separately. ``rutf8.unichr_as_utf8`` is the motivating case: its
    contract is "<= 0x10FFFF **and** not a surrogate unless
    ``allow_surrogates=True``". A ``0 <= value <= 0x10FFFF`` range check
    discharges the first clause and says nothing about the second, so scoring
    the call ACCEPTABLE on a numeric hint alone hides a live bug -- confirmed on
    ``pypy/objspace/std/newformat.py:847``, where exactly that range check sits
    three lines above an unguarded call and ``format(0xD800, 'c')`` leaks
    ``SystemError`` on PyPy 7.3.23 where CPython returns ``U+D800``.

    Helpers with no ``clauses`` key keep the original single-clause behaviour.
    """
    clauses = helper.get("clauses")
    exc_hints = helper.get("exception_guard_hints", [])
    if not clauses:
        if _has_guard_hint(func_text, helper.get("guard_pattern_hints", [])):
            return []
        if _guarded_by_except(call, parent, exc_hints):
            return []
        return ["precondition"]

    undischarged = []
    for clause in clauses:
        kwarg = clause.get("discharged_by_kwarg")
        if kwarg and _call_has_truthy_kwarg(call, kwarg):
            continue
        if _has_guard_hint(func_text, clause.get("text_hints", [])):
            continue
        # A value read out of an already-validated utf8 string is in range by
        # construction; pypy/objspace/std/formatting.py:351 feeds
        # rutf8.codepoint_at_pos straight through, and demanding a numeric
        # range check there is noise. Narrow and provenance-specific on
        # purpose -- it does not clear struct/formatiterator.py:175, whose
        # value comes from four raw unpacked bytes.
        if _has_guard_hint(func_text, clause.get("provenance_hints", [])):
            continue
        if _guarded_by_except(call, parent, exc_hints):
            continue
        undischarged.append(clause.get("name", "precondition"))
    return undischarged


def _check_file(path: Path, project_root: Path, helpers: list[dict]) -> list[dict]:
    result = parse_rpython_file(path)
    if result.parser != "ast" or result.ast_tree is None:
        return []

    tree = result.ast_tree
    rel = relative_to_root(path, project_root)
    source_lines = result.source.splitlines()
    findings: list[dict] = []

    # Build a parent map once so each call can be attributed to its
    # INNERMOST enclosing function only. An earlier version iterated
    # ast.walk() per FunctionDef, which also visits calls inside any
    # nested function defined within it -- a call inside a nested function
    # got processed twice, once correctly scoped to the nested function and
    # once incorrectly scoped to the outer function's much broader text
    # (which can contain an unrelated guard pattern belonging to a sibling
    # nested function, producing a false ACCEPTABLE alongside the correct
    # CONSIDER for the same call). Confirmed on real code:
    # pypy/objspace/std/newformat.py:547's nested _lit() function.
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    def _innermost_function(node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        cur = node
        while cur in parent:
            cur = parent[cur]
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return cur
        return None

    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        for helper in helpers:
            if not _call_matches_helper(call, helper["qualname"]):
                continue
            enclosing = _innermost_function(call)
            if enclosing is None:
                # Module-level call, no enclosing function -- rare, but
                # handle rather than crash. No function-scoped text to
                # check a guard against, so report as unguarded.
                func_text = ""
            else:
                func_text = _function_source_lines(enclosing, source_lines)
            undischarged = _undischarged_clauses(call, func_text, helper, parent)
            if not undischarged:
                findings.append(
                    _finding(
                        "unvalidated-helper-call-guarded",
                        "ACCEPTABLE",
                        "low",
                        call,
                        f"call to {helper['qualname']} in a function that appears to "
                        f"contain a matching guard pattern -- likely fine, but the "
                        f"guard-hint match is textual proximity, not real data-flow; "
                        f"verify the guard actually covers this specific call's argument",
                        helper["precondition"],
                    )
                )
            else:
                findings.append(
                    _finding(
                        "unvalidated-helper-call",
                        "CONSIDER",
                        "high",
                        call,
                        f"call to {helper['qualname']} leaves the "
                        f"{'/'.join(undischarged)} clause(s) of its precondition "
                        f"undischarged -- no proactive check in the enclosing "
                        f"function, no allow_surrogates-style opt-in at the call, and "
                        f"no reactive except-handler around it. This is the exact "
                        f"shape of 3 confirmed real bugs: struct.unpack('u',...), "
                        f"multibyte codec setstate(), and format(0xD800, 'c')",
                        helper["precondition"],
                    )
                )

    for f in findings:
        f["file"] = rel
    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    resolved = Path(target).resolve()
    project_root = find_project_root(resolved)
    scan_root = resolved

    discovery = discover(target)
    helpers = _load_helpers()

    if discovery.get("is_pypy_checkout"):
        checkout_root = Path(discovery["checkout_root"])
        files_by_layer = discovery.get("files_by_layer", {})
        # This scanner's real surface (per both confirmed bugs) is
        # pypy/module specifically -- app-level-reachable code calling into
        # rpython.rlib helpers. rlib/jit/gc/interpreter/objspace could in
        # principle also call these helpers, but neither confirmed bug is
        # there, and interp2app reachability (the thing that makes a bad
        # value "attacker-controlled") is a pypy/module-and-interpreter
        # concern specifically.
        in_scope_rel_paths = [
            rel
            for layer in ("module", "interpreter", "objspace")
            for rel in files_by_layer.get(layer, [])
        ]
        files = [checkout_root / rel for rel in in_scope_rel_paths]
        files_total = len(files)
        if max_files > 0 and files_total > max_files:
            files = files[:max_files]
    else:
        files, files_total = collect_python_files(scan_root, max_files)

    non_test_files = [f for f in files if "/test/" not in str(f) and "/tests/" not in str(f)]

    findings: list[dict] = []
    for path in non_test_files:
        findings.extend(_check_file(path, project_root, helpers))

    findings = deduplicate_findings(findings)
    envelope = build_pypy_envelope(discovery, findings, functions_analyzed=len(non_test_files))
    envelope["pypy_info"]["sensitive_helpers_tracked"] = [h["qualname"] for h in helpers]
    return envelope


def main() -> None:
    target, max_files = parse_common_args(sys.argv[1:])
    result = analyze(target, max_files=max_files)
    emit(result)


if __name__ == "__main__":
    main()
