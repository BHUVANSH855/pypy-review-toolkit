#!/usr/bin/env python3
"""Detect a method missing the guard its same-named siblings on other classes have.

**Corrects a real design mistake, found by re-verifying against the correct
checkout.** `scan_sibling_guard_consistency.py` (built first) checks for a
guard missing *within one class*, using `self._check_*(...)` calls as the
guard signal. Re-verifying danzin's confirmed bug #5 against
`release-pypy3.11-v7.3.23` (the actual tag matching the fuzzing target --
`main`, used for every earlier scanner in this toolkit, turned out to be
tracking Python 2.7.18, not 3.11.15 at all) showed the real bug doesn't
match that shape:

- The guard is an inline `if self.w_raw:` / `if self.fd >= 0:` truthiness
  check, not a delegated `self._check_*(...)` call.
- The defect is **cross-class**, not within-class: `_dealloc_warn_w` is
  defined separately on 5 different classes across `pypy/module/_io/`
  (`W_IOBase` -- no-op stub, correctly unguarded; `W_BufferedReader`/etc via
  `interp_bufferedio.py`'s mixin -- guards `self.w_raw`;
  `interp_win32consoleio.py` -- guards `self.buf`; `interp_fileio.py` --
  guards `self.fd`/`self.closefd`; `interp_textio.py`'s
  `W_TextIOWrapper` -- **no guard at all**, calls
  `space.call_method(self.w_buffer, "_dealloc_warn", w_source)`
  unconditionally). Four of five same-named definitions guard their
  relevant field before use; one doesn't.

This scanner checks the real shape: collect every method definition across
all in-scope files, grouped by method name. For names with 2+ definitions
on different classes, flag any definition that accesses `self.<field>`
(a call or attribute access, not just a read) without a preceding
`if self.<field>` truthiness/None-check anywhere earlier in the same
function, when at least one same-named sibling definition does have such a
guard. A bare `pass`/stub definition (like `W_IOBase`'s) is not flagged --
it doesn't access any field at all, so there's nothing to guard.

`scan_sibling_guard_consistency.py` is kept as a separate, valid check --
it catches a related but genuinely different pattern (within-class
guard-convention deviation, confirmed real on `W_BytesIO`/`W_StringIO`/
`W_MemoryView`, none of which is the confirmed bug itself). This scanner
is the one that actually reproduces catching bug #5.

Usage:
    python scan_cross_class_method_guard.py [path] [--max-files N]
"""

# covers: interpreter, objspace, module

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


def _self_fields_accessed(method: ast.FunctionDef) -> set[str]:
    """Fields accessed via self.X anywhere in *method* (calls or plain reads)."""
    fields = set()
    for node in ast.walk(method):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
            fields.add(node.attr)
    return fields


def _resource_fields_accessed(method: ast.FunctionDef) -> set[str]:
    """Return self-fields that look like resource/lifecycle state.

    The cross-class guard heuristic is intended for resource lifecycle
    mismatches, such as a detached/closed wrapper being accessed without
    checking its backing resource first. Generic object state used for
    conversion, protocol dispatch, or ordinary value storage should not
    be
    treated as evidence of such a lifecycle invariant.
    """
    fields = _self_fields_accessed(method)

    # These are value/protocol fields rather than resource-lifecycle state.
    # They produced false positives when generic self-field access was used
    # as the cross-class comparison signal.
    non_resource_fields = {
        "num",
        "intval",
        "_value",
    }

    return fields - non_resource_fields


def _guarded_fields(method: ast.FunctionDef) -> set[str]:
    """Fields that appear in an `if self.X` / `if not self.X` / `if self.X is
    None`-shaped test anywhere in *method* -- the inline truthiness/None-check
    guard pattern confirmed as the real one danzin's bug #5 needs
    (`if self.w_raw:`, `if self.fd >= 0 and self.closefd:`, `if buf:`
    where `buf = self.buf`)."""
    guarded: set[str] = set()
    for node in ast.walk(method):
        if not isinstance(node, ast.If):
            continue
        for test_node in ast.walk(node.test):
            if isinstance(test_node, ast.Attribute) and isinstance(test_node.value, ast.Name) and test_node.value.id == "self":
                guarded.add(test_node.attr)
    # Also handle the `buf = self.buf; if buf:` local-alias shape
    # (interp_win32consoleio.py) -- a simple single-assignment alias
    # followed by a truthiness check on the alias name.
    alias_of: dict[str, str] = {}
    for node in ast.walk(method):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value = node.value
            if (
                isinstance(target, ast.Name)
                and isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name)
                and value.value.id == "self"
            ):
                alias_of[target.id] = value.attr
    for node in ast.walk(method):
        if not isinstance(node, ast.If):
            continue
        for test_node in ast.walk(node.test):
            if isinstance(test_node, ast.Name) and test_node.id in alias_of:
                guarded.add(alias_of[test_node.id])
    return guarded


def _has_explicit_guard(method: ast.FunctionDef) -> bool:
    """Return whether *method* explicitly calls a self._check_* guard."""
    for node in ast.walk(method):
        if not isinstance(node, ast.Call):
            continue

        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "self"
            and func.attr.startswith("_check_")
        ):
            return True

    return False


def _is_trivial_stub(method: ast.FunctionDef) -> bool:
    """True if *method*'s body is just `pass`, a docstring, or a bare `return`."""
    meaningful = [
        s
        for s in method.body
        if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str))
    ]
    if not meaningful:
        return True
    if len(meaningful) == 1 and isinstance(meaningful[0], (ast.Pass, ast.Return)):
        return True
    return False


def _collect_methods(tree: ast.Module, rel_path: str) -> list[dict]:
    """Collect every method def in *tree* with its class name, for cross-file grouping."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if isinstance(item, ast.FunctionDef):
                out.append(
                    {
                        "class_name": node.name,
                        "method_name": item.name,
                        "node": item,
                        "file": rel_path,
                    }
                )
    return out


# Method names too generic/universal to compare across arbitrary classes --
# every class defines __init__ by design, and its "guard" needs are
# structurally unrelated between unrelated classes. Confirmed necessary by
# running this scanner without the exclusion: __init__ alone produced 1868
# of 1868 findings, comparing e.g. W_Super.__init__ against State.__init__,
# classes with nothing to do with each other beyond the shared name.
_EXCLUDED_METHOD_NAMES = frozenset(
    {
        "__init__",
        "descr_init",
        "__repr__",
        "descr_repr",
        "__del__",
        "__new__",
        "__str__",
        "descr_str",
        "__eq__",
        "__hash__",
        "__len__",
    }
)


# Base classes and mixins frequently provide intentionally generic/default
# implementations. Comparing those implementations against concrete sibling
# classes creates false positives because the base method is not necessarily
# expected to enforce the concrete class's resource-lifecycle invariant.
#
# The cross-class guard heuristic is intended to find deviations between
# concrete implementations of the same PyPy wrapper operation, not differences
# between an abstract/base implementation and its subclasses.
_BASE_CLASS_NAME_MARKERS = (
    "Base",
    "Mixin",
)


def _is_base_or_mixin_class(class_name: str) -> bool:
    """Return whether *class_name* looks like a base/helper implementation."""
    return (
        class_name.endswith(_BASE_CLASS_NAME_MARKERS)
        or class_name.startswith("W_Abstract")
        or class_name in {
            "W_IOBase",
            "W_TextIOBase",
            "W_BufferedIOBase",
            "BufferedMixin",
        }
    )


def _is_lifecycle_exempt_method(method_name: str) -> bool:
    """Return whether *method_name* has intentionally idempotent semantics.

    Close operations are allowed to run on an already-closed object, so an
    implementation of close_w() should not be required to establish an
    open-state guard before performing the close operation itself.
    """
    return method_name == "close_w"


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
            for layer in ("module", "interpreter", "objspace", "lib_pypy")
            for rel in files_by_layer.get(layer, [])
        ]
        files = [checkout_root / rel for rel in in_scope_rel_paths]
        files_total = len(files)
        if max_files > 0 and files_total > max_files:
            files = files[:max_files]
    else:
        files, files_total = collect_python_files(scan_root, max_files)

    non_test_files = [f for f in files if "/test/" not in str(f) and "/tests/" not in str(f)]

    all_methods: list[dict] = []
    for path in non_test_files:
        result = parse_rpython_file(path)
        if result.parser != "ast" or result.ast_tree is None:
            continue
        rel = relative_to_root(path, project_root)
        all_methods.extend(_collect_methods(result.ast_tree, rel))

    # Group by (directory, method_name) rather than method_name alone --
    # "sibling classes" means classes in the same subsystem (the _io
    # module's 5 files implementing _dealloc_warn_w all live in
    # pypy/module/_io/), not any two classes anywhere in the tree that
    # happen to share a method name. Confirmed necessary the same way the
    # name exclusion was: without directory scoping, unrelated modules'
    # same-named helper methods would still be compared.
    by_dir_and_name: dict[tuple[str, str], list[dict]] = {}
    for m in all_methods:
        if m["method_name"] in _EXCLUDED_METHOD_NAMES:
            continue
        if not m["method_name"].endswith("_w"):
            # Confirmed necessary by running this scanner without the
            # restriction: directory-scoping alone still produced 666
            # findings, dominated by unrelated classes in the same
            # directory that happen to implement the same generic Python
            # protocol method independently (__contains__, close, get,
            # update -- e.g. contextvars.Context and _dbm's dbm class,
            # compared as "siblings" for __contains__ despite having
            # nothing to do with each other). PyPy's own convention is
            # that interp2app-wrapped methods are suffixed `_w`
            # (read_w, close_w, _dealloc_warn_w) -- restricting to that
            # suffix keeps genuine PyPy-specific lifecycle-hook
            # comparisons (which is what the real confirmed bug is) while
            # dropping the generic-protocol false positives, since none
            # of __contains__/close/get/update/__getitem__ match it.
            continue
        directory = str(Path(m["file"]).parent)
        by_dir_and_name.setdefault((directory, m["method_name"]), []).append(m)

    findings: list[dict] = []
    for (directory, method_name), defs in by_dir_and_name.items():
        if _is_lifecycle_exempt_method(method_name):
            continue
        # Only interesting when the SAME method name is defined on 2+
        # DIFFERENT classes -- multiple overrides in an inheritance chain
        # within one file also produce multiple defs of the same name, but
        # the cross-class-in-different-files pattern is what bug #5
        # actually is.
        distinct_classes = {(d["class_name"], d["file"]) for d in defs}
        if len(distinct_classes) < 2:
            continue

        # Base classes and mixins commonly provide generic/default methods.
        # Their lack of a concrete resource guard is not, by itself, evidence
        # that a concrete sibling implementation is missing a guard.
        concrete_defs = [
            d for d in defs
            if not _is_base_or_mixin_class(d["class_name"])
        ]

        if len({
            (d["class_name"], d["file"])
            for d in concrete_defs
        }) < 2:
            continue

        guarded_defs = []
        unguarded_defs = []
        for d in concrete_defs:
            method = d["node"]
            if _is_trivial_stub(method):
                continue  # W_IOBase's `pass` shape -- nothing to guard.

            # An explicit self._check_* call is already strong evidence that
            # the method follows PyPy's resource/lifecycle guard convention.
            # Do this before field-based analysis because many wrapper
            # operations access their actual state indirectly through helpers
            # such as self.read() or self.write().
            if _has_explicit_guard(method):
                guarded_defs.append(d)
                continue

            accessed = _resource_fields_accessed(method)
            if not accessed:
                continue

            guarded = _guarded_fields(method)
            if accessed & guarded:
                guarded_defs.append(d)
            else:
                unguarded_defs.append(d)

        if not guarded_defs or not unguarded_defs:
            # Need at least one real example of each to say there's a
            # real, deviated-from convention.
            continue

        for d in unguarded_defs:
            findings.append(
                {
                    **_finding(
                        "cross-class-method-guard-missing",
                        "CONSIDER",
                        "high",
                        d["node"],
                        f"{d['class_name']}.{method_name}() accesses self.* fields with "
                        f"no guard, while {len(guarded_defs)} other class(es) defining "
                        f"the same method name do guard theirs "
                        f"({', '.join(sorted({g['class_name'] for g in guarded_defs}))[:200]}) "
                        f"-- this is the exact shape of danzin's confirmed bug #5 "
                        f"(W_TextIOWrapper._dealloc_warn_w segfaulting on a detached "
                        f"wrapper because 4 sibling classes' same-named method all guard "
                        f"their equivalent field and this one doesn't)",
                        f"file: {d['file']}",
                    ),
                    "file": d["file"],
                }
            )

    findings = deduplicate_findings(findings)
    return build_pypy_envelope(discovery, findings, functions_analyzed=len(non_test_files))


def main() -> None:
    target, max_files = parse_common_args(sys.argv[1:])
    result = analyze(target, max_files=max_files)
    emit(result)


if __name__ == "__main__":
    main()
