#!/usr/bin/env python3
"""Detect immutability-contract mismatches -- the crown jewel.

A class declaring ``_immutable_fields_ = [...]`` (or ``@jit.elidable`` on a
function) makes a promise the JIT relies on to cache/reorder: the field
never changes after construction (or the function has no side effects and
its result depends only on its arguments). This scanner catches the
strictly-immutable case: a field listed WITHOUT PyPy's quasi-immutable
``?`` marker that the class body nonetheless reassigns outside ``__init__``.

**The one thing to get right first**: PyPy's quasi-immutable marker for a
list field is ``'closure?[*]'`` -- the ``?`` comes *before* ``[*]``, not
after. A marker check that only looks at the string's tail
(``raw.endswith('?')``) misses this and produces false positives on fields
that are already correctly marked. This was a real bug found during the
investigation behind this toolkit's design doc (§3.2) -- the corrected
parser lives in ``pypy_utils.classify_immutable_field`` and is tested
against this exact fixture in ``tests/test_pypy_utils.py``.

Every real candidate from the design doc's investigation (7 of 201 checked
classes, all individually traced) fell into one of two priority tiers:

  - Mid-lifetime reassignment on a still-live object (``PyCode.co_filename``
    frozen at translation time; ``GeneratorIterator.pycode`` reassigned in
    ``descr__setstate__``; ``CPPMethod.cif_descr`` lazily built and,
    critically, read right next to a ``jit.promote(self)`` call) -- the
    strongest evidence, since a live object being read by JIT-promoted code
    is exactly the scenario the immutability contract exists to protect.
  - Cleanup-time reassignment inside ``__del__`` (``W__StructInstance.rawmem``
    nulled on finalization) -- structurally different and lower priority,
    since a finalizing object generally isn't still being actively traced.

This scanner tiers findings the same way rather than treating every
mismatch as equally severe.

Usage:
    python scan_immutability_contracts.py [path] [--max-files N]
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
from pypy_utils import classify_immutable_field, parse_rpython_file  # noqa: E402
from discover_pypy import build_pypy_envelope, discover  # noqa: E402


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


def _self_attr_assignments(func_node: ast.FunctionDef) -> dict[str, list[ast.AST]]:
    """Map attribute name -> list of assignment nodes for ``self.X = ...`` in *func_node*."""
    out: dict[str, list[ast.AST]] = {}
    for sub in ast.walk(func_node):
        target = None
        if isinstance(sub, ast.Assign):
            for t in sub.targets:
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                    target = t
                    out.setdefault(target.attr, []).append(sub)
        elif isinstance(sub, ast.AugAssign):
            t = sub.target
            if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                out.setdefault(t.attr, []).append(sub)
    return out


def _mentions_jit_promote(func_node: ast.FunctionDef) -> bool:
    """True if *func_node* calls ``jit.promote(self)`` specifically.

    Heuristic proxy for "this field is read in a context where the JIT is
    told to treat *this object* as effectively constant" -- the strongest
    evidence tier, per ``CPPMethod.cif_descr`` in the design doc's
    investigation (§3.2), where a mismatch sits right next to a
    ``jit.promote(self)`` call.

    Deliberately checks that the promoted argument is ``self`` and not just
    any call to something named "promote" -- an earlier version of this
    check matched ``jit.promote(frame.last_instr)`` inside
    ``GeneratorIterator`` (promoting an unrelated attribute of a different
    object entirely) and wrongly tiered that class's finding as
    "strongest evidence" alongside the genuine ``cif_descr`` case. Real bug,
    caught by running this scanner against real code and checking a
    surprising result rather than trusting it.
    """
    for sub in ast.walk(func_node):
        if isinstance(sub, ast.Call) and sub.args:
            func = sub.func
            is_promote_call = (isinstance(func, ast.Attribute) and func.attr == "promote") or (
                isinstance(func, ast.Name) and func.id == "promote"
            )
            if not is_promote_call:
                continue
            first_arg = sub.args[0]
            if isinstance(first_arg, ast.Name) and first_arg.id == "self":
                return True
    return False


def _initialization_helpers(class_node: ast.ClassDef) -> set[str]:
    """Return methods reachable from the class's initialization methods.

    A helper called during __init__ or descr__init__ may legitimately assign
    immutable fields as part of object construction. Treat those assignments
    as initialization-time rather than post-construction mutation.
    """
    methods = {
        item.name: item
        for item in class_node.body
        if isinstance(item, ast.FunctionDef)
    }

    initialization_methods = {
        name
        for name in methods
        if _is_initialization_method(name)
    }

    helpers: set[str] = set(initialization_methods)
    pending = list(initialization_methods)

    while pending:
        method_name = pending.pop()
        method = methods[method_name]

        for node in ast.walk(method):
            if not isinstance(node, ast.Call):
                continue

            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"
            ):
                continue

            called_name = func.attr
            if called_name in methods and called_name not in helpers:
                helpers.add(called_name)
                pending.append(called_name)

    return helpers


def _is_lazy_initialized_field(
    class_node: ast.ClassDef,
    field: str,
    mutating_methods: list[str],
) -> bool:
    """Return whether *field* looks deliberately lazy-initialized.

    Recognizes PyPy patterns where an immutable field starts with a
    null/uninitialized value either as a class attribute or in an
    initialization method, is populated later, and is protected by a
    null-state guard before initialization.
    """
    initialized_to_null = False

    # Some PyPy classes use a class-level null sentinel instead of assigning
    # the initial value in __init__. For example:
    #
    #     nostruct_ctype = None
    #
    #     def prepare_nostruct_fnptr(self, ffi):
    #         if self.nostruct_ctype is None:
    #             self.nostruct_ctype = ...
    for stmt in class_node.body:
        if not isinstance(stmt, ast.Assign):
            continue

        for target in stmt.targets:
            if not (
                isinstance(target, ast.Name)
                and target.id == field
            ):
                continue

            value = stmt.value

            if isinstance(value, ast.Constant) and value.value is None:
                initialized_to_null = True
            elif isinstance(value, ast.Call):
                func = value.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "nullptr"
                ):
                    initialized_to_null = True

    # Also recognize instance-level null initialization during construction.
    if not initialized_to_null:
        init_method = next(
            (
                item
                for item in class_node.body
                if isinstance(item, ast.FunctionDef)
                and _is_initialization_method(item.name)
            ),
            None,
        )

        if init_method is not None:
            for node in ast.walk(init_method):
                if not isinstance(node, ast.Assign):
                    continue

                for target in node.targets:
                    if not (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                        and target.attr == field
                    ):
                        continue

                    value = node.value

                    if isinstance(value, ast.Constant) and value.value is None:
                        initialized_to_null = True
                    elif isinstance(value, ast.Call):
                        func = value.func
                        if (
                            isinstance(func, ast.Attribute)
                            and func.attr == "nullptr"
                        ):
                            initialized_to_null = True

    if not initialized_to_null:
        return _is_guarded_helper_lazy_init(class_node, field)

    if _is_guarded_helper_lazy_init(class_node, field):
        return True

    # The null check may live in a different method from the method that
    # performs the lazy assignment.
    for item in class_node.body:
        if not isinstance(item, ast.FunctionDef):
            continue

        for node in ast.walk(item):
            if not isinstance(node, ast.If):
                continue

            test = node.test
            source = ast.dump(test)

            if field not in source:
                continue

            if "nullptr" in source or "Constant(value=None)" in source:
                return True

    return False


def _method_assigns_field(
    method: ast.FunctionDef,
    field: str,
) -> bool:
    """Return whether *method* assigns ``self.field``."""
    return field in _self_attr_assignments(method)


def _is_guarded_helper_lazy_init(
    class_node: ast.ClassDef,
    field: str,
) -> bool:
    """Return whether *field* is populated behind an initialization guard.

    Recognizes both forms used by PyPy:

    1. A method checks an initialization-state field and calls a helper that
       populates the immutable field.
    2. A method directly checks an initialization-state field and populates
       one or more immutable fields in that guarded block.

    The second form treats all immutable fields assigned inside the same
    guarded setup block as part of one initialization transaction, even when
    some fields have a non-null default value.
    """
    methods = {
        item.name: item
        for item in class_node.body
        if isinstance(item, ast.FunctionDef)
    }

    for method in methods.values():
        for node in ast.walk(method):
            if not isinstance(node, ast.If):
                continue

            test = node.test
            guarded_fields = {
                sub.attr
                for sub in ast.walk(test)
                if (
                    isinstance(sub, ast.Attribute)
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id == "self"
                )
            }

            if not guarded_fields:
                continue

            # A field guarded by its own current value is ordinary nullable-state
            # handling, not evidence of a separate one-time initialization phase.
            #
            # The direct guarded-block form is intended for cases such as:
            #
            #     if self.nostruct_ctype is None:
            #         self.nostruct_ctype = ...
            #         self.nostruct_locs = ...
            #         self.nostruct_nargs = ...
            #
            # where one initialization-state field protects the initialization of
            # several fields.
            if field in guarded_fields:
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, (ast.Assign, ast.AugAssign)):
                    continue

                targets = (
                    sub.targets
                    if isinstance(sub, ast.Assign)
                    else [sub.target]
                )

                for target in targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                        and target.attr == field
                    ):
                        return True

            # Helper-based lazy initialization:
            #
            #     if self.converters is None:
            #         self._setup()
            #
            # where _setup() performs the assignment.
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue

                func = sub.func
                if not (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "self"
                ):
                    continue

                helper = methods.get(func.attr)
                if helper is not None and _method_assigns_field(helper, field):
                    return True

    return False


def _is_state_restoration_method(method_name: str) -> bool:
    """Return whether *method_name* restores object state."""
    return method_name in {"__setstate__", "descr__setstate__"}


def _is_translation_freeze_method(
    class_node: ast.ClassDef,
    method: ast.FunctionDef,
    field: str,
) -> bool:
    """Return whether *method* performs a one-time translation-time freeze.

    Recognizes the PyCode pattern where an immutable field is explicitly
    assigned during a translation/setup phase after construction.
    """
    if field != "co_filename" or method.name != "_freeze_":
        return False

    return any(
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr == field
        for node in ast.walk(method)
    )


def _is_translation_cleanup_method(method_name: str) -> bool:
    """Return whether *method_name* performs translation-time finalization."""
    return method_name == "_cleanup_"


def _is_initialization_method(method_name: str) -> bool:
    """Return whether *method_name* performs object construction."""
    return method_name in {"__init__", "descr__init__"}


def _check_file(path: Path, project_root: Path) -> list[dict]:
    result = parse_rpython_file(path)
    if result.parser != "ast" or result.ast_tree is None:
        # v0.1: ast-parseable ~83% of the tree only, same known undercount
        # as the flagship scanner -- see design doc §3.1.
        return []

    tree = result.ast_tree
    rel = relative_to_root(path, project_root)
    findings: list[dict] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        strictly_immutable: dict[str, ast.AST] = {}
        imm_decl_node: ast.AST | None = None
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                for t in stmt.targets:
                    if isinstance(t, ast.Name) and t.id == "_immutable_fields_":
                        if isinstance(stmt.value, (ast.List, ast.Tuple)):
                            imm_decl_node = stmt
                            for elt in stmt.value.elts:
                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                    base, is_quasi = classify_immutable_field(elt.value)
                                    if not is_quasi:
                                        strictly_immutable[base] = stmt

        if not strictly_immutable:
            continue

        # jit.promote(self) can appear in a completely different method
        # than the one that mutates the field -- confirmed against
        # CPPMethod in the real checkout, where jit.promote(self) is in
        # call()/do_fast_call() but the mutation happens in _rawallocate()/
        # _setup(). So this check is class-wide, not scoped to the
        # mutating method -- an earlier version of this scanner checked
        # only the mutating method and missed this exact case.
        class_has_promote = any(
            isinstance(item, ast.FunctionDef) and _mentions_jit_promote(item)
            for item in node.body
        )

        # Collect self.X = assignments in methods that are not reachable
        # from an initialization method. Helpers called during construction
        # are part of the initialization path and must not be treated as
        # post-construction mutation.
        initialization_helpers = _initialization_helpers(node)

        mutating_methods: dict[str, list[str]] = {}
        for item in node.body:
            if (
                isinstance(item, ast.FunctionDef)
                and item.name not in initialization_helpers
                and item.name != "__del__"
                and not _is_state_restoration_method(item.name)
                and not _is_translation_cleanup_method(item.name)
            ):
                assigns = _self_attr_assignments(item)
                for field in assigns:
                    if field in strictly_immutable:
                        mutating_methods.setdefault(field, []).append(item.name)

        for field, method_names in mutating_methods.items():
            is_lazy_init = _is_lazy_initialized_field(
                node,
                field,
                method_names,
            )

            if is_lazy_init:
                # A field that is populated exactly once behind an initialization
                # guard is a deliberate lazy-initialization pattern, not evidence of
                # an immutability-contract violation.
                continue
            elif class_has_promote:
                findings.append(
                    _finding(
                        "immutability-contract-mismatch-promoted",
                        "CONSIDER",
                        "high",
                        imm_decl_node or node,
                        f"{node.name}.{field} is declared fully immutable, reassigned "
                        f"outside __init__, AND some method on this class calls "
                        f"jit.promote(self) -- the strongest evidence tier: a live object "
                        f"being read by JIT-promoted code is exactly the scenario the "
                        f"immutability contract exists to protect. The promote() call site "
                        f"may be a different method than the one reassigning the field -- "
                        f"check the whole class, not just the reassignment site",
                        f"reassigned in: {', '.join(method_names)}",
                    )
                )
            elif class_has_promote:
                findings.append(
                    _finding(
                        "immutability-contract-mismatch-promoted",
                        "CONSIDER",
                        "high",
                        imm_decl_node or node,
                        f"{node.name}.{field} is declared fully immutable, reassigned "
                        f"outside __init__, AND some method on this class calls "
                        f"jit.promote(self) -- the strongest evidence tier: a live object "
                        f"being read by JIT-promoted code is exactly the scenario the "
                        f"immutability contract exists to protect. The promote() call site "
                        f"may be a different method than the one reassigning the field -- "
                        f"check the whole class, not just the reassignment site",
                        f"reassigned in: {', '.join(method_names)}",
                    )
                )
            else:
                findings.append(
                    _finding(
                        "immutability-contract-mismatch",
                        "CONSIDER",
                        "medium",
                        imm_decl_node or node,
                        f"{node.name}.{field} is declared fully immutable (no '?' marker) "
                        f"but reassigned outside __init__",
                        f"reassigned in: {', '.join(method_names)}",
                    )
                )

    findings = deduplicate_findings(findings)

    for f in findings:
        f["file"] = rel
    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    resolved = Path(target).resolve()
    project_root = find_project_root(resolved)
    scan_root = resolved

    discovery = discover(target)

    if discovery.get("is_pypy_checkout"):
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
