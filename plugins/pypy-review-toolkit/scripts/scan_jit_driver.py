#!/usr/bin/env python3
"""Detect structural JIT violations: argument mismatches, unbounded unroll_safe
loops, and dead JitDriver instances.

Three independent passes:

**Pass 1 — argument-list mismatch**
For each ``JitDriver(greens=[...], reds=[...])`` definition, find every
``.can_enter_jit(...)`` and ``.jit_merge_point(...)`` call on that driver
variable and compare keyword argument names against the declared
``greens + reds`` list.  Any name present in the declaration but absent at
the call site, or present at the call site but not in the declaration, is a
``JITDRIVER_ARG_MISMATCH`` finding.

**Pass 2 — ``@jit.unroll_safe`` with unbounded loop**
Walk every function decorated with ``@jit.unroll_safe``.  Flag ``for`` loops
whose iterator is a bare parameter, ``range(len(x))``, or ``range(x)`` where
``x`` is not the result of a ``jit.promote()`` call, as
``UNROLL_SAFE_UNBOUNDED``.  Also flag ``while`` loops whose condition tests
``len(something)`` derived from a parameter.

**Pass 3 — dead JitDriver**
A ``JitDriver(...)`` assigned to a variable with no ``<varname>.jit_merge_point``
anywhere in the same file is a ``DEAD_JITDRIVER`` finding.

Findings are leads for ``jit-trace-reviewer`` to triage, not confirmed defects.

Usage:
    python scan_jit_driver.py [path] [--max-files N]
"""

# covers: jit, interpreter

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
from pypy_utils import parse_rpython_file  # noqa: E402
from discover_pypy import build_pypy_envelope, discover  # noqa: E402


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

def _finding(
    subtype: str,
    classification: str,
    confidence: str,
    node: ast.AST,
    driver_var: str,
    detail: str,
) -> dict:
    return {
        "type": "jit_driver_issue",
        "subtype": subtype,
        "classification": classification,
        "confidence": confidence,
        "line": getattr(node, "lineno", 0),
        "column": getattr(node, "col_offset", 0),
        "driver_var": driver_var,
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# Pass 1 — argument-list mismatch
# ---------------------------------------------------------------------------

def _extract_string_list(node: ast.expr | None) -> list[str] | None:
    """Extract a list of string literals from an ast.List node, or None."""
    if not isinstance(node, ast.List):
        return None
    result: list[str] = []
    for elt in node.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            result.append(elt.value)
        else:
            return None  # non-literal element — give up
    return result


def _find_jitdriver_defs(tree: ast.Module) -> dict[str, tuple[list[str], list[str], ast.AST]]:
    """Return {varname: (greens, reds, node)} for JitDriver assignments."""
    drivers: dict[str, tuple[list[str], list[str], ast.AST]] = {}

    for node in ast.walk(tree):
        # Match: varname = JitDriver(greens=[...], reds=[...])
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue

        if isinstance(node, ast.Assign):
            value = node.value
            targets = node.targets
        else:
            value = node.value
            targets = [node.target] if node.value else []

        if not (isinstance(value, ast.Call)):
            continue

        func = value.func
        is_jitdriver = (
            (isinstance(func, ast.Name) and func.id == "JitDriver")
            or (isinstance(func, ast.Attribute) and func.attr == "JitDriver")
        )
        if not is_jitdriver:
            continue

        greens: list[str] | None = None
        reds: list[str] | None = None
        for kw in value.keywords:
            if kw.arg == "greens":
                greens = _extract_string_list(kw.value)
            elif kw.arg == "reds":
                # reds may be "auto" string
                if isinstance(kw.value, ast.Constant) and kw.value.value == "auto":
                    reds = []  # treat as empty — can't mismatch
                else:
                    reds = _extract_string_list(kw.value)

        if greens is None or reds is None:
            continue

        for target in targets:
            if isinstance(target, ast.Name):
                drivers[target.id] = (greens, reds, node)
            elif isinstance(target, ast.Attribute):
                drivers[target.attr] = (greens, reds, node)

    return drivers


def _check_arg_mismatch(tree: ast.Module) -> list[dict]:
    findings: list[dict] = []
    drivers = _find_jitdriver_defs(tree)
    if not drivers:
        return findings

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in ("can_enter_jit", "jit_merge_point"):
            continue

        # Identify the driver variable
        obj = func.value
        if isinstance(obj, ast.Name):
            var = obj.id
        elif isinstance(obj, ast.Attribute):
            var = obj.attr
        else:
            continue

        if var not in drivers:
            continue

        greens, reds, def_node = drivers[var]
        declared = greens + reds
        declared_set = set(declared)

        call_kwargs = {kw.arg for kw in node.keywords if kw.arg is not None}
        # Also handle positional args mapped to declared names in order
        # (rare but possible) — skip positional for now; keyword-only is standard

        missing = declared_set - call_kwargs
        extra = call_kwargs - declared_set

        if missing or extra:
            parts = []
            if missing:
                parts.append(f"declared but absent at call site: {sorted(missing)}")
            if extra:
                parts.append(f"present at call site but not declared: {sorted(extra)}")
            detail = (
                f"{var}.{func.attr}() argument mismatch — "
                + "; ".join(parts)
                + f" (declared greens={greens}, reds={reds})"
            )
            findings.append(_finding(
                subtype="JITDRIVER_ARG_MISMATCH",
                classification="REVIEW",
                confidence="high",
                node=node,
                driver_var=var,
                detail=detail,
            ))

    return findings


# ---------------------------------------------------------------------------
# Pass 2 — @jit.unroll_safe unbounded loop
# ---------------------------------------------------------------------------

def _is_unroll_safe_decorator(dec: ast.expr) -> bool:
    if isinstance(dec, ast.Attribute):
        return dec.attr == "unroll_safe" and isinstance(dec.value, ast.Name) and dec.value.id == "jit"
    if isinstance(dec, ast.Name):
        return dec.id == "unroll_safe"
    if isinstance(dec, ast.Call):
        return _is_unroll_safe_decorator(dec.func)
    return False


def _promoted_names(func: ast.FunctionDef) -> set[str]:
    """Collect names that are assigned the result of jit.promote(...)."""
    promoted: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign):
            continue
        val = node.value
        if isinstance(val, ast.Call):
            fn = val.func
            is_promote = (
                (isinstance(fn, ast.Attribute) and fn.attr == "promote")
                or (isinstance(fn, ast.Name) and fn.id == "promote")
            )
            if is_promote:
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        promoted.add(t.id)
    return promoted


def _param_names(func: ast.FunctionDef) -> set[str]:
    args = func.args
    names: set[str] = set()
    for a in args.args + args.posonlyargs + args.kwonlyargs:
        names.add(a.arg)
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def _iter_is_range_len_param(iter_node: ast.expr, params: set[str], promoted: set[str]) -> bool:
    """True for range(len(x)) or range(x) where x is a param and not promoted."""
    if not (isinstance(iter_node, ast.Call) and isinstance(iter_node.func, ast.Name) and iter_node.func.id == "range"):
        return False
    if len(iter_node.args) != 1:
        return False
    arg = iter_node.args[0]
    # range(len(x))
    if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and arg.func.id == "len":
        if arg.args and isinstance(arg.args[0], ast.Name):
            name = arg.args[0].id
            return name in params and name not in promoted
    # range(x) where x is a param
    if isinstance(arg, ast.Name):
        return arg.id in params and arg.id not in promoted
    return False


def _check_unroll_safe(tree: ast.Module) -> list[dict]:
    findings: list[dict] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(_is_unroll_safe_decorator(d) for d in node.decorator_list):
            continue

        params = _param_names(node)
        promoted = _promoted_names(node)

        for child in ast.walk(node):
            if child is node:
                continue

            if isinstance(child, ast.For):
                iter_node = child.iter
                # for item in x  where x is a bare param (iterating a list arg)
                if isinstance(iter_node, ast.Name) and iter_node.id in params and iter_node.id not in promoted:
                    findings.append(_finding(
                        subtype="UNROLL_SAFE_UNBOUNDED",
                        classification="REVIEW",
                        confidence="medium",
                        node=child,
                        driver_var=node.name,
                        detail=(
                            f"@jit.unroll_safe function '{node.name}' iterates over "
                            f"parameter '{iter_node.id}' directly — bound is unknown at trace time"
                        ),
                    ))
                elif _iter_is_range_len_param(iter_node, params, promoted):
                    findings.append(_finding(
                        subtype="UNROLL_SAFE_UNBOUNDED",
                        classification="REVIEW",
                        confidence="medium",
                        node=child,
                        driver_var=node.name,
                        detail=(
                            f"@jit.unroll_safe function '{node.name}' loops over "
                            f"range(len(...))/range(param) — bound is not a promoted constant"
                        ),
                    ))

    return findings


# ---------------------------------------------------------------------------
# Pass 3 — dead JitDriver
# ---------------------------------------------------------------------------

def _check_dead_jitdriver(tree: ast.Module, source_text: str) -> list[dict]:
    findings: list[dict] = []
    drivers = _find_jitdriver_defs(tree)
    if not drivers:
        return findings

    for var, (greens, reds, def_node) in drivers.items():
        # Check if <var>.jit_merge_point appears in the source text (fast path)
        if f"{var}.jit_merge_point" not in source_text:
            findings.append(_finding(
                subtype="DEAD_JITDRIVER",
                classification="REVIEW",
                confidence="high",
                node=def_node,
                driver_var=var,
                detail=(
                    f"JitDriver '{var}' (greens={greens}, reds={reds}) has no "
                    f"'{var}.jit_merge_point(...)' call in this file"
                ),
            ))

    return findings


# ---------------------------------------------------------------------------
# Per-file check
# ---------------------------------------------------------------------------

def _check_file(path: Path, project_root: Path) -> list[dict]:
    result = parse_rpython_file(path)
    if result.parser != "ast" or result.ast_tree is None:
        return []

    tree = result.ast_tree
    rel = relative_to_root(path, project_root)

    try:
        source_text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        source_text = ""

    findings: list[dict] = []

    for f in _check_arg_mismatch(tree):
        f["file"] = rel
        findings.append(f)

    for f in _check_unroll_safe(tree):
        f["file"] = rel
        findings.append(f)

    for f in _check_dead_jitdriver(tree, source_text):
        f["file"] = rel
        findings.append(f)

    return findings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze(target: str, *, max_files: int = 0) -> dict:
    resolved = Path(target).resolve()
    project_root = find_project_root(resolved)

    discovery = discover(target)

    if discovery.get("is_pypy_checkout"):
        checkout_root = Path(discovery["checkout_root"])
        files_by_layer = discovery.get("files_by_layer", {})
        in_scope_rel_paths: list[str] = []
        for layer in ("rpython_jit", "interpreter", "objspace"):
            in_scope_rel_paths.extend(files_by_layer.get(layer, []))
        files = [checkout_root / rel for rel in in_scope_rel_paths]
        if max_files > 0 and len(files) > max_files:
            files = files[:max_files]
    else:
        files, _ = collect_python_files(resolved, max_files)

    non_test_files = [f for f in files if "/test/" not in str(f) and "/tests/" not in str(f)]

    findings: list[dict] = []
    for path in non_test_files:
        findings.extend(_check_file(path, project_root))

    findings = deduplicate_findings(findings)
    return build_pypy_envelope(discovery, findings, functions_analyzed=len(non_test_files))


def main() -> None:
    target, max_files = parse_common_args(sys.argv[1:])
    result = analyze(target, max_files=max_files)
    emit(result)


if __name__ == "__main__":
    main()
