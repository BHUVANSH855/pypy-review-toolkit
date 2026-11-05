#!/usr/bin/env python3
"""Detect GC-discipline violations in RPython source.

Two independent passes:

**Pass 1 — @rgc.no_collect violation detection**
Walk every function/method decorated with ``@rgc.no_collect`` or
``@rgc.no_collect_with_trace``.  Inside the body, flag direct GC-managed
allocations that make the ``@no_collect`` promise implausible:

- ``lltype.malloc(...)`` without ``flavor='raw'``
- Direct construction of a ``W_``-prefixed class
- List or dict display literals not inside a ``we_are_translated()`` guard
- Calls whose callee name ends in ``_alloc``, ``_new``, or ``malloc``

Conditional allocations under ``if not we_are_translated():`` guards
(which only run on CPython, never in the translated binary) produce at most
``NO_COLLECT_SUSPECT``; unconditional allocations produce
``NO_COLLECT_VIOLATION``.

**Pass 2 — raw-memory without trace hook**
Walk every ``W_``-prefixed class.  If ``__init__`` or any method assigns
the result of ``lltype.malloc(flavor='raw')`` or ``ffi.new(...)`` to
``self.*``, and ``rgc.register_custom_trace_hook`` is absent from the
entire class body, emit ``RAW_ALLOC_NO_TRACE_HOOK``.  Allocations into
local variables (not ``self.*``) produce ``RAW_ALLOC_SUSPECT``.

Findings require manual source investigation — the scanner produces leads,
not confirmed defects.

Usage:
    python scan_gc_hints.py [path] [--max-files N]
"""

# covers: interpreter, rlib, gc

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
# Shared helpers
# ---------------------------------------------------------------------------

def _finding(
    subtype: str,
    classification: str,
    confidence: str,
    node: ast.AST,
    function: str,
    detail: str,
) -> dict:
    return {
        "type": "gc_hint_violation",
        "subtype": subtype,
        "classification": classification,
        "confidence": confidence,
        "line": getattr(node, "lineno", 0),
        "column": getattr(node, "col_offset", 0),
        "function": function,
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# Pass 1 helpers — @rgc.no_collect detection
# ---------------------------------------------------------------------------

def _is_no_collect_decorator(dec: ast.expr) -> bool:
    """Return True for @rgc.no_collect or @rgc.no_collect_with_trace."""
    if isinstance(dec, ast.Attribute):
        return (
            dec.attr in ("no_collect", "no_collect_with_trace")
            and isinstance(dec.value, ast.Name)
            and dec.value.id == "rgc"
        )
    if isinstance(dec, ast.Name):
        return dec.id in ("no_collect", "no_collect_with_trace")
    # @rgc.no_collect() — called form
    if isinstance(dec, ast.Call):
        return _is_no_collect_decorator(dec.func)
    return False


def _is_we_are_translated_guard(test: ast.expr) -> bool:
    """True for ``we_are_translated()`` and ``not we_are_translated()``."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _is_we_are_translated_guard(test.operand)
    if isinstance(test, ast.Call):
        func = test.func
        if isinstance(func, ast.Name) and func.id == "we_are_translated":
            return True
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "we_are_translated"
        ):
            return True
    return False


def _build_parent_map(root: ast.AST) -> dict[int, ast.AST]:
    parent: dict[int, ast.AST] = {}
    for node in ast.walk(root):
        for child in ast.iter_child_nodes(node):
            parent[id(child)] = node
    return parent


def _inside_translated_guard(node: ast.AST, parent: dict[int, ast.AST]) -> bool:
    """True if *node* is nested inside an ``if [not] we_are_translated():`` branch."""
    cur = parent.get(id(node))
    while cur is not None:
        if isinstance(cur, ast.If) and _is_we_are_translated_guard(cur.test):
            return True
        cur = parent.get(id(cur))
    return False


def _is_gc_malloc(call: ast.Call) -> bool:
    """True for ``lltype.malloc(...)`` without flavor='raw'."""
    func = call.func
    is_malloc = (
        (isinstance(func, ast.Attribute) and func.attr == "malloc")
        or (isinstance(func, ast.Name) and func.id == "malloc")
    )
    if not is_malloc:
        return False
    for kw in call.keywords:
        if kw.arg == "flavor" and isinstance(kw.value, ast.Constant) and kw.value.value == "raw":
            return False
    return True


def _is_w_class_construction(call: ast.Call) -> bool:
    """True for direct ``W_Foo(...)`` calls."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id.startswith("W_")
    if isinstance(func, ast.Attribute):
        return func.attr.startswith("W_")
    return False


def _callee_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _is_alloc_named_call(call: ast.Call) -> bool:
    """True for calls ending in _alloc, _new, or malloc (heuristic)."""
    name = _callee_name(call)
    return name.endswith(("_alloc", "_new", "malloc")) and not _is_gc_malloc(call)


def _check_no_collect_function(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    parent: dict[int, ast.AST],
    func_qname: str,
) -> list[dict]:
    findings: list[dict] = []

    for node in ast.walk(func):
        if node is func:
            continue

        subtype = None
        detail = None

        if isinstance(node, ast.Call):
            if _is_gc_malloc(node):
                subtype_base = "lltype.malloc() without flavor='raw' inside @no_collect function"
                detail = subtype_base
            elif _is_w_class_construction(node):
                name = _callee_name(node)
                detail = f"direct {name}() construction inside @no_collect function"
            elif _is_alloc_named_call(node):
                name = _callee_name(node)
                detail = f"call to '{name}' (allocator-named) inside @no_collect function"
            else:
                continue

        elif isinstance(node, (ast.List, ast.Dict)):
            detail = f"{'list' if isinstance(node, ast.List) else 'dict'} literal inside @no_collect function"

        else:
            continue

        in_guard = _inside_translated_guard(node, parent)
        subtype = "NO_COLLECT_SUSPECT" if in_guard else "NO_COLLECT_VIOLATION"
        confidence = "medium" if in_guard else "high"

        findings.append(_finding(
            subtype=subtype,
            classification="REVIEW",
            confidence=confidence,
            node=node,
            function=func_qname,
            detail=detail,
        ))

    return findings


# ---------------------------------------------------------------------------
# Pass 2 helpers — raw-memory without trace hook
# ---------------------------------------------------------------------------

def _is_raw_malloc(call: ast.Call) -> bool:
    """True for ``lltype.malloc(flavor='raw')``."""
    func = call.func
    is_malloc = (
        (isinstance(func, ast.Attribute) and func.attr == "malloc")
        or (isinstance(func, ast.Name) and func.id == "malloc")
    )
    if not is_malloc:
        return False
    for kw in call.keywords:
        if kw.arg == "flavor" and isinstance(kw.value, ast.Constant) and kw.value.value == "raw":
            return True
    return False


def _is_ffi_new(call: ast.Call) -> bool:
    """True for ``ffi.new(...)``."""
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "new"
        and isinstance(func.value, ast.Name)
        and func.value.id == "ffi"
    )


def _assigns_to_self(assign_node: ast.AST) -> bool:
    """True if the assignment target is ``self.*``."""
    targets: list[ast.expr] = []
    if isinstance(assign_node, ast.Assign):
        targets = list(assign_node.targets)
    elif isinstance(assign_node, (ast.AnnAssign, ast.AugAssign)):
        targets = [assign_node.target]
    for t in targets:
        if (
            isinstance(t, ast.Attribute)
            and isinstance(t.value, ast.Name)
            and t.value.id == "self"
        ):
            return True
    return False


def _has_trace_hook(cls: ast.ClassDef) -> bool:
    """True if ``rgc.register_custom_trace_hook`` appears anywhere in the class."""
    for node in ast.walk(cls):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "register_custom_trace_hook":
                return True
            if isinstance(func, ast.Name) and func.id == "register_custom_trace_hook":
                return True
    return False


def _check_w_class(cls: ast.ClassDef) -> list[dict]:
    findings: list[dict] = []
    if _has_trace_hook(cls):
        return findings

    for method in ast.walk(cls):
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # Build parent map scoped to this method
        method_parent: dict[int, ast.AST] = {}
        for n in ast.walk(method):
            for child in ast.iter_child_nodes(n):
                method_parent[id(child)] = n

        for node in ast.walk(method):
            if not isinstance(node, ast.Call):
                continue
            if not (_is_raw_malloc(node) or _is_ffi_new(node)):
                continue

            alloc_desc = "lltype.malloc(flavor='raw')" if _is_raw_malloc(node) else "ffi.new()"

            # Determine whether result goes to self.*
            parent_node = method_parent.get(id(node))
            to_self = False
            if parent_node is not None and isinstance(parent_node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                to_self = _assigns_to_self(parent_node)

            if to_self:
                subtype = "RAW_ALLOC_NO_TRACE_HOOK"
                confidence = "high"
                detail = (
                    f"{alloc_desc} in {cls.name}.{method.name} assigned to self.* "
                    f"without rgc.register_custom_trace_hook in class body"
                )
            else:
                subtype = "RAW_ALLOC_SUSPECT"
                confidence = "medium"
                detail = (
                    f"{alloc_desc} in {cls.name}.{method.name} — lifetime unclear; "
                    f"no rgc.register_custom_trace_hook in class body"
                )

            findings.append(_finding(
                subtype=subtype,
                classification="REVIEW",
                confidence=confidence,
                node=node,
                function=f"{cls.name}.{method.name}",
                detail=detail,
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
    findings: list[dict] = []

    # Build a global parent map for translated-guard detection (Pass 1)
    global_parent = _build_parent_map(tree)

    # Pass 1 — @rgc.no_collect functions
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(_is_no_collect_decorator(d) for d in node.decorator_list):
            continue
        # Determine qualified name (best effort: class prefix if nested)
        func_name = node.name
        for f in _check_no_collect_function(node, global_parent, func_name):
            f["file"] = rel
            findings.append(f)

    # Pass 2 — W_* classes with raw alloc and no trace hook
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name.startswith("W_"):
            for f in _check_w_class(node):
                f["file"] = rel
                findings.append(f)

    return findings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze(target: str, *, max_files: int = 0) -> dict:
    resolved = Path(target).resolve()
    project_root = find_project_root(resolved)
    scan_root = resolved

    discovery = discover(target)

    if discovery.get("is_pypy_checkout"):
        checkout_root = Path(discovery["checkout_root"])
        files_by_layer = discovery.get("files_by_layer", {})
        # GC discipline spans rlib, gc layer, interpreter
        in_scope_rel_paths: list[str] = []
        for layer in ("rpython_rlib", "rpython_gc", "rpython_jit", "interpreter", "objspace"):
            in_scope_rel_paths.extend(files_by_layer.get(layer, []))
        files = [checkout_root / rel for rel in in_scope_rel_paths]
        if max_files > 0 and len(files) > max_files:
            files = files[:max_files]
    else:
        files, _ = collect_python_files(scan_root, max_files)

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
