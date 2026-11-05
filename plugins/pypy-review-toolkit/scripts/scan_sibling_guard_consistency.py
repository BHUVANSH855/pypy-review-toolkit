#!/usr/bin/env python3
"""Detect app-exposed methods missing a class's established guard convention.

Shape D from design doc §9.1/§9.2, per danzin's fusil-confirmed bug #5:
`TextIOWrapper.detach()` followed by `._dealloc_warn(None)` segfaults.
pypy#5123's fix added a guard to the analogous method on the *buffered* IO
layer (`interp_bufferedio.py`) but never touched the *text* IO layer
(`interp_textio.py`) -- an incomplete fix applied to one layer of a wrapper
stack, leaving one method without the guard every sibling method on its
class has.

**Generalizes an idea already working elsewhere in this toolkit**:
`interp-app-boundary-checker`'s same-function-asymmetry signal (two checks
in one function, one correct convention and one not) proved to be the
strongest real signal that scanner found. This is the same idea one level
up -- N methods on a class, N-1 call the class's own `self._check_*(...)`
guard convention before doing their work, one doesn't.

**The distinguishing signal is `TypeDef`/`interp2app` registration, not
underscore-prefix naming.** A naive "does this method call a guard" check
run against a real class (`W_TextIOWrapper`) produces 9 false candidates
out of 34 methods -- but 8 of those 9 are purely internal RPython helper
methods (`_read_chunk`, `_ensure_data`, etc.) that are only ever called
*from* an already-guarded exposed method, and legitimately don't need their
own guard. The real distinguishing signal, confirmed against the actual
checkout, is whether a method is registered as `interp2app(ClassName.method)`
inside the class's `TypeDef(...)` -- i.e., actually reachable from running
app-level Python code. This scanner only evaluates methods in that set.

**v0.1 limitation, stated plainly**: "guard" is detected as any call to
`self._check_*(...)` -- a real, confirmed PyPy naming convention
(`_check_init`, `_check_attached`, `_check_closed` all exist in the real
checkout), but a class could plausibly use a differently-named guard
pattern this scanner won't recognize. A convention is only "established"
for a class when at least 2 of its exposed methods use it -- a single
guarded method isn't enough evidence the class has a real convention to
deviate from.

Usage:
    python scan_sibling_guard_consistency.py [path] [--max-files N]
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

_MIN_METHODS_TO_ESTABLISH_CONVENTION = 2
_MIN_GUARD_PROPORTION = 0.5


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


def _find_exposed_methods(tree: ast.Module) -> set[tuple[str, str]]:
    """Find every (ClassName, method_name) registered via interp2app(ClassName.method_name)."""
    exposed: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "interp2app":
            if node.args and isinstance(node.args[0], ast.Attribute):
                attr = node.args[0]
                if isinstance(attr.value, ast.Name):
                    exposed.add((attr.value.id, attr.attr))
    return exposed


def _calls_guard(method: ast.FunctionDef) -> bool:
    for sub in ast.walk(method):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
            if sub.func.attr.startswith("_check_") and isinstance(sub.func.value, ast.Name) and sub.func.value.id == "self":
                return True
    return False


def _is_staticmethod(method: ast.FunctionDef) -> bool:
    """Return whether *method* is declared with @staticmethod."""
    return any(
        isinstance(dec, ast.Name) and dec.id == "staticmethod"
        for dec in method.decorator_list
    )


def _is_close_method(method: ast.FunctionDef) -> bool:
    """Return whether *method* is an exposed close operation."""
    return method.name in {"close", "close_w"}


def _calls_guarded_helper(
    method: ast.FunctionDef,
    methods: dict[str, ast.FunctionDef],
    seen: set[str] | None = None,
) -> bool:
    """Return whether *method* calls a helper that eventually calls a guard."""
    if seen is None:
        seen = set()

    if method.name in seen:
        return False
    seen.add(method.name)

    for sub in ast.walk(method):
        if not isinstance(sub, ast.Call):
            continue

        func = sub.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "self"
        ):
            continue

        called_name = func.attr

        if called_name.startswith("_check_"):
            return True

        helper = methods.get(called_name)
        if helper is not None and _calls_guarded_helper(helper, methods, seen):
            return True

    return False


def _calls_view_method(method: ast.FunctionDef) -> bool:
    """Return whether *method* delegates directly to self.view."""
    for sub in ast.walk(method):
        if not isinstance(sub, ast.Call):
            continue

        func = sub.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Attribute)
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "self"
            and func.value.attr == "view"
        ):
            return True

    return False


def _calls_guarded_method(
    method: ast.FunctionDef,
    guarded_names: set[str],
) -> bool:
    """Return whether *method* directly calls a guarded sibling method."""
    for sub in ast.walk(method):
        if not isinstance(sub, ast.Call):
            continue

        func = sub.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "self"
            and func.attr in guarded_names
        ):
            return True

    return False


def _check_file(path: Path, project_root: Path) -> list[dict]:
    result = parse_rpython_file(path)
    if result.parser != "ast" or result.ast_tree is None:
        return []

    tree = result.ast_tree
    rel = relative_to_root(path, project_root)
    findings: list[dict] = []

    exposed = _find_exposed_methods(tree)
    if not exposed:
        return []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        class_methods = {
            m.name: m for m in node.body if isinstance(m, ast.FunctionDef)
        }
        class_exposed_names = {
            method_name for cls_name, method_name in exposed if cls_name == node.name
        }
        if not class_exposed_names:
            continue

        guarded_exposed: list[str] = []
        unguarded_exposed: list[ast.FunctionDef] = []
        for name in class_exposed_names:
            method = class_methods.get(name)
            if method is None or method.name in ("__init__", "descr_init"):
                # descr_init/__init__ legitimately don't need a guard --
                # there's nothing to be detached/closed/invalid yet.
                continue

            if _is_staticmethod(method):
                # Static methods have no instance self and therefore cannot follow
                # the class's self._check_* guard convention.
                continue

            if _is_close_method(method):
                # Closing is intentionally allowed on an already-closed object and
                # therefore does not follow the self._check_* guard convention.
                continue

            if _calls_guard(method):
                guarded_exposed.append(name)
            else:
                unguarded_exposed.append(method)

        if len(guarded_exposed) < _MIN_METHODS_TO_ESTABLISH_CONVENTION:
            # Not enough evidence this class has a real guard convention to
            # deviate from -- avoid flooding findings on classes that
            # simply don't use this pattern at all.
            continue

        total_evaluated = len(guarded_exposed) + len(unguarded_exposed)
        guard_proportion = len(guarded_exposed) / total_evaluated if total_evaluated else 0
        if guard_proportion < _MIN_GUARD_PROPORTION:
            # Confirmed real limitation, found by running this scanner
            # against W_IOBase: an ABSTRACT base class can have a handful
            # of methods that happen to use a guard for unrelated reasons
            # (isatty_w, __enter__, __iter__) while most of its ~20 other
            # methods are no-op/stub defaults meant to be overridden by
            # concrete subclasses -- flagging all of those as "missing the
            # established guard" is wrong, because there IS no established
            # convention on this particular class, just a minority of
            # methods that happen to guard. Requiring a majority of
            # evaluated methods to be guarded (not just an absolute count
            # of 2) distinguishes "this class has a real convention, one
            # method deviates" (W_TextIOWrapper: 21/26, ~81%) from "this
            # class barely uses the pattern at all" (W_IOBase: 5/20, ~25%).
            continue

        guarded_names = set(guarded_exposed)

        for method in unguarded_exposed:
            if _calls_guarded_method(method, guarded_names):
                continue

            if _calls_guarded_helper(method, class_methods):
                continue

            if _calls_view_method(method):
                continue

            findings.append(
                _finding(
                    "sibling-guard-missing",
                    "CONSIDER",
                    "high",
                    method,
                    f"{node.name}.{method.name}() is app-exposed (registered via "
                    f"interp2app) but doesn't call any self._check_*(...) guard, "
                    f"while {len(guarded_exposed)} other exposed methods on the same "
                    f"class do ({', '.join(sorted(guarded_exposed)[:5])}"
                    f"{'...' if len(guarded_exposed) > 5 else ''}) -- this is the exact "
                    f"shape of danzin's confirmed bug #5 (_dealloc_warn_w segfaulting "
                    f"on a detached TextIOWrapper, because pypy#5123's fix only guarded "
                    f"the analogous method on a sibling wrapper layer)",
                    f"guarded siblings: {sorted(guarded_exposed)}",
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

    if discovery.get("is_pypy_checkout"):
        checkout_root = Path(discovery["checkout_root"])
        files_by_layer = discovery.get("files_by_layer", {})
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
        findings.extend(_check_file(path, project_root))

    findings = deduplicate_findings(findings)
    return build_pypy_envelope(discovery, findings, functions_analyzed=len(non_test_files))


def main() -> None:
    target, max_files = parse_common_args(sys.argv[1:])
    result = analyze(target, max_files=max_files)
    emit(result)


if __name__ == "__main__":
    main()
