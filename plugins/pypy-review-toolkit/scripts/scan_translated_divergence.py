#!/usr/bin/env python3
"""Detect ``we_are_translated()`` arm divergence -- the flagship check.

``rpython.rlib.objectmodel.we_are_translated()`` branches PyPy's own source
between two execution modes: interpreted-under-CPython (the fast test-suite
path) and translated-to-C (the real interpreter). The two arms are, by
construction, never exercised by the same test run -- this has no analog in
CPython or RustPython, which is why it's this toolkit's flagship (design doc
§3.3), currently held as a working hypothesis rather than confirmed.

The census behind the design doc found 284 real branch sites (166 files) and
a split the original plan didn't anticipate: only 59 (21%) have an
``else``/``elif`` arm to diff. The remaining 225 (79%) are a bare
``if not we_are_translated(): assert ...`` -- every sampled instance was a
debug-only sanity check, not two behaviors to diff. That shape is
mechanically recognizable (body is only assert/raise statements) and is
suppressed here automatically -- an agent spending judgment on 225 sites a
run before this suppress rule existed would have drowned in noise for no
signal, exactly the outcome every census behind this toolkit's design
warned against.

Two-arm sites get a POLICY-vs-CONSIDER-vs-FIX classification based on how
different the two arms actually look, not a flat "has both arms" flag --
``rpython/rlib/rgil.py``'s ``EmulatedGilHolder`` shim (both arms call
different real implementations, both produce/consume real values) is
deliberate and substantial, not the same thing as a bare debug print, and
the classification here reflects that distinction rather than lumping every
two-arm site under one label.

Usage:
    python scan_translated_divergence.py [path] [--max-files N]
"""

from __future__ import annotations

import ast
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
from pypy_utils import (  # noqa: E402
    is_debug_adjacent_body,
    is_debug_only_body,
    is_we_are_translated_call,
    parse_rpython_file,
)
from discover_pypy import build_pypy_envelope, discover  # noqa: E402

# Names that, alone or with a simple call, don't count as "real logic" for
# the two-arm divergence-shape heuristic below -- calling only these in an
# arm doesn't distinguish a substantive alternate implementation from
# instrumentation, so a two-arm site where BOTH arms only touch these names
# is treated more conservatively (POLICY, not CONSIDER) even though it has
# two arms.
_LOW_SIGNAL_CALL_NAMES = frozenset({"print", "debug_print", "log", "warn", "os"})


def _call_names_in(body: list[ast.stmt]) -> set[str]:
    """Collect the (unqualified) names of every function/method called in *body*."""
    names: set[str] = set()
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _arm_shape(body: list[ast.stmt]) -> dict:
    """Summarize one arm's shape for the divergence heuristic.

    Cheap, deliberately not a semantic diff -- the agent reading a CONSIDER
    or FIX finding does the real comparison. This just decides which bucket
    a two-arm site falls into so the agent isn't handed all 59 with equal
    weight.
    """
    has_return_or_raise = any(isinstance(s, (ast.Return, ast.Raise)) for s in body)
    call_names = _call_names_in(body)
    substantive_calls = call_names - _LOW_SIGNAL_CALL_NAMES
    return {
        "has_return_or_raise": has_return_or_raise,
        "call_names": call_names,
        "substantive_calls": substantive_calls,
        "stmt_count": len(body),
    }


def _is_translation_boundary_arm(body: list[ast.stmt]) -> bool:
    """Return whether an arm looks like an intentional translation boundary.

    PyPy deliberately uses different implementations for some
    we_are_translated() branches. Common examples include:

    - low-level ``llop`` operations used only by translated code;
    - ``AssertGreenFailed``-style Python-side test behavior;
    - explicitly non-translatable helper calls;
    - untranslated emulation helpers;
    - translated low-level casts/operations with a Python-side emulation
      counterpart;
    - translated GC/runtime operations with a Python-side test representation.
    """
    call_names = _call_names_in(body)

    if "llop" in call_names:
        return True

    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Call):
            func = node.func

            if isinstance(func, ast.Attribute):
                if func.attr in {
                    "debug_fatalerror",
                    "debug_print_traceback",
                    "gc_fq_register",
                }:
                    return True

                # These are commonly used to cross the translated/untranslated
                # representation boundary.  They are especially common in
                # RPython low-level and GC helpers.
                if func.attr in {
                    "cast_ptr_to_int",
                    "cast_opaque_ptr",
                    "cast_instance_to_base_ptr",
                    "cast_base_ptr_to_instance",
                    "cast_gcref_to_instance",
                    "cast_instance_to_gcref",
                    "cast_gcref_to_int",
                    "raw_storage_getitem",
                    "raw_storage_setitem",
                    "_raw_storage_getitem_unchecked",
                    "_raw_storage_setitem_unchecked",
                    "bare_setarrayitem",
                }:
                    return True

            if isinstance(func, ast.Name):
                if func.id in {
                    "cast_instance_to_gcref",
                    "cast_gcref_to_int",
                    "try_cast_gcref_to_instance",
                    "cast_base_ptr_to_instance",
                    "cast_instance_to_base_ptr",
                    "raw_storage_getitem",
                    "raw_storage_setitem",
                    "_raw_storage_getitem_unchecked",
                    "_raw_storage_setitem_unchecked",
                }:
                    return True

        if isinstance(node, ast.Name):
            if node.id in {
                "AssertGreenFailed",
                "ContinueRunningNormally",
            }:
                return True

        if isinstance(node, ast.Attribute):
            if node.attr in {
                "_nontranslated_run_directly",
            }:
                return True

    return False


def _classify_two_arm(
    if_body: list[ast.stmt],
    else_body: list[ast.stmt],
) -> tuple[str, str]:
    """Return (classification, reason) for a two-arm we_are_translated() site.

    - FIX: the arms' control-flow shape is observably inconsistent (one
      returns/raises, the other doesn't), unless the difference is explained
      by a recognized translation-boundary implementation.
    - CONSIDER: both arms call different substantive (non-low-signal)
      functions -- a real difference that may be deliberate.
    - POLICY: everything else with two arms -- e.g. both arms only touch
      low-signal names, or the arms look structurally similar.
    """
    if_shape = _arm_shape(if_body)
    else_shape = _arm_shape(else_body)

    if_boundary = _is_translation_boundary_arm(if_body)
    else_boundary = _is_translation_boundary_arm(else_body)

    if if_shape["has_return_or_raise"] != else_shape["has_return_or_raise"]:
        if if_boundary or else_boundary:
            return (
                "CONSIDER",
                "arms have inconsistent return/raise control-flow shape, "
                "but one arm matches a recognized translation-boundary pattern; "
                "verify that the alternate implementation is intentional",
            )

        return "FIX", "arms have inconsistent return/raise control-flow shape"

    if if_shape["substantive_calls"] and else_shape["substantive_calls"]:
        if if_shape["substantive_calls"] != else_shape["substantive_calls"]:
            if if_boundary or else_boundary:
                return (
                    "CONSIDER",
                    "arms call different substantive functions, but one arm "
                    "matches a recognized translation-boundary pattern; verify "
                    "that the alternate implementation is intentional",
                )

            return (
                "CONSIDER",
                "arms call different substantive functions "
                f"({sorted(if_shape['substantive_calls'])} vs "
                f"{sorted(else_shape['substantive_calls'])}) -- possible deliberate "
                "alternate implementation (e.g. an emulation shim); verify intent",
            )

    return "POLICY", "arms present but low structural difference; likely intentional"


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


def _check_file(path: Path, project_root: Path) -> list[dict]:
    result = parse_rpython_file(path)
    if result.parser != "ast" or result.ast_tree is None:
        # v0.1: this check runs on the ast-parseable ~83% of the tree.
        # Extending it to the tree-sitter-fallback 17% needs a tree-sitter-
        # native walk (design doc §3.1 notes decorator inspection has the
        # same gap) -- not yet implemented, tracked as a known undercount
        # rather than silently skipped.
        return []

    tree = result.ast_tree
    rel = relative_to_root(path, project_root)
    findings: list[dict] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        is_match, negated = is_we_are_translated_call(node.test)
        if not is_match:
            continue

        has_else = bool(node.orelse) and not (
            len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If)
        )
        # An elif is structurally a nested If in node.orelse; treat it as
        # "no else" for this checker's purposes -- a real elif chain on
        # we_are_translated() wasn't seen in the census and would need its
        # own handling rather than being silently mis-shaped here.

        if not has_else:
            # Single-arm site: the body that actually runs when the
            # predicate is true (or, if negated, when it's false) is what
            # we check for the debug-only/debug-adjacent shape.
            body = node.body
            if is_debug_only_body(body):
                findings.append(
                    _finding(
                        "we-are-translated-single-arm-debug-only",
                        "ACCEPTABLE",
                        "high",
                        node,
                        "single-arm we_are_translated() guard whose body is only "
                        "assert/raise statements -- flat debug-only shape",
                    )
                )
            elif is_debug_adjacent_body(body, negated):
                findings.append(
                    _finding(
                        "we-are-translated-single-arm-debug-adjacent",
                        "ACCEPTABLE",
                        "medium",
                        node,
                        "single-arm we_are_translated() guard matching a broadened "
                        "debug-adjacent shape (wrapped assert, or early-return-when-"
                        "translated before an assertion) -- see pypy_utils.is_debug_adjacent_body",
                    )
                )
            else:
                findings.append(
                    _finding(
                        "we-are-translated-single-arm-other",
                        "CONSIDER",
                        "medium",
                        node,
                        "single-arm we_are_translated() guard with a body that isn't "
                        "a recognized debug-only/debug-adjacent shape -- the majority "
                        "case (~78% of the real census once the checkout was actually "
                        "scanned), needs real per-site attention rather than blanket "
                        "suppression",
                    )
                )
            continue

        # Two-arm site.
        if_body = node.body if not negated else node.orelse
        else_body = node.orelse if not negated else node.body
        classification, reason = _classify_two_arm(if_body, else_body)
        conf = "high" if classification == "FIX" else "medium"
        findings.append(
            _finding(
                "we-are-translated-two-arm-divergence",
                classification,
                conf,
                node,
                "we_are_translated() branch with both arms present -- real "
                "candidate for behavioral divergence between translated and "
                "untranslated execution",
                reason,
            )
        )

    for f in findings:
        f["file"] = rel
        f["function"] = _enclosing_function_name(tree, f["line"])
    return findings


def _enclosing_function_name(tree: ast.Module, lineno: int) -> str:
    """Best-effort: the innermost function/method containing *lineno*, or ''."""
    best: str = ""
    best_start = -1
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = node.lineno
            end = getattr(node, "end_lineno", None) or start
            if start <= lineno <= end and start > best_start:
                best = node.name
                best_start = start
    return best


def analyze(target: str, *, max_files: int = 0) -> dict:
    resolved = Path(target).resolve()
    project_root = find_project_root(resolved)
    scan_root = resolved

    discovery = discover(target)

    if discovery.get("is_pypy_checkout"):
        # Scan only in-scope layers (design doc §3.6): rlib, jit, gc,
        # interpreter, objspace, module. Without this filter the scanner
        # would also walk lib-python/ (the CPython-compatible stdlib PyPy
        # ships, explicitly out of scope per §1's project identity), plus
        # vendored test dependencies and out-of-scope annotator/rtyper/
        # translator internals -- caught by actually running this against
        # the real checkout rather than assuming collect_python_files's
        # default excludes were enough.
        checkout_root = Path(discovery["checkout_root"])
        files_by_layer = discovery.get("files_by_layer", {})
        in_scope_rel_paths = [
            rel
            for layer in sorted(discovery.get("in_scope_layers", []))
            for rel in files_by_layer.get(layer, [])
        ]
        files = [checkout_root / rel for rel in in_scope_rel_paths]
        files_total = len(files)
        if max_files > 0 and files_total > max_files:
            files = files[:max_files]
    else:
        # Not a recognized PyPy checkout -- fall back to a plain scan of
        # whatever was pointed at, same as any other scanner would.
        files, files_total = collect_python_files(scan_root, max_files)

    findings: list[dict] = []
    for path in files:
        findings.extend(_check_file(path, project_root))

    findings = deduplicate_findings(findings)
    return build_pypy_envelope(discovery, findings, functions_analyzed=len(files))


def main() -> None:
    target, max_files = parse_common_args(sys.argv[1:])
    result = analyze(target, max_files=max_files)
    emit(result)


if __name__ == "__main__":
    main()
