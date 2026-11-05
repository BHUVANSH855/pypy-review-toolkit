#!/usr/bin/env python3
"""Detect interp/app boundary leaks: raw exceptions where OperationError is expected.

``pypy/interpreter/gateway.py``'s ``unwrap_spec``/``interp2app`` wrap
interpreter-level (RPython) functions for calling from the Python program
PyPy is running. A raw Python exception (``raise ValueError(...)``) raised
in interp-level code reachable from app-level execution leaks CPython-hosted
exception machinery into what's supposed to be a faithfully emulated
app-level exception -- correct only by accident under CPython-hosted
testing, and not guaranteed to survive translation.

**Primary signal: same-function asymmetry, not a flat raise-site count.**
The investigation behind this toolkit's design doc found its strongest real
candidate this way -- ``pypy/interpreter/pyframe.py``'s
``initialize_frame_scopes`` has two structurally similar internal-
consistency checks a few lines apart: one correctly uses
``oefmt``/``OperationError``, the other raises a raw ``ValueError``. A flat
count of raw-exception sites (97 in the census, nearly as many as the 107
correct sites) doesn't discriminate well on its own; a function using both
conventions for similar checks is a much sharper signal, and it's checkable
statically -- no call-graph pass required.

**Secondary check: a lightweight caller-reachability search.** The
investigation resolved ``pypy/interpreter/argument.py``'s ``fixedunpack``
as a false positive -- a raw ``ValueError`` with a docstring that says
"raise a real ValueError," with zero production callers in the tree (the
only call site is its own test, which explicitly asserts the exception).
This scanner does the cheap version of that same check: a text search for
the function's name as a call (``.funcname(`` or bare ``funcname(``)
anywhere in non-test in-scope source. It's a heuristic, not real call-graph
analysis (it can't see conditional/reflective calls, and matches on name
alone rather than resolving which class the call is actually against) --
zero matches is treated as a real signal (ACCEPTABLE, matching what
happened with fixedunpack), but a nonzero count is not proof of reachability
either, just "worth a closer look," same CONSIDER posture as everything
else this scanner reports.

Usage:
    python scan_interp_app_boundary.py [path] [--max-files N]
"""

# covers: interpreter, module, objspace

from __future__ import annotations

import ast
import re
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

# Raw builtin exceptions worth flagging when raised directly by name in
# interp-level code. Not exhaustive -- these are the ones the census behind
# this toolkit's design doc actually found in real raise sites.
_RAW_EXCEPTION_NAMES = frozenset(
    {
        "ValueError",
        "TypeError",
        "KeyError",
        "IndexError",
        "AttributeError",
        "RuntimeError",
        "NotImplementedError",
        "Exception",
        "OverflowError",
        "ZeroDivisionError",
        "StopIteration",
        "ImportError",
        "OSError",
    }
)


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


def _is_unreachable_not_implemented(node: ast.Raise) -> bool:
    """Return whether *node* is an internal unreachable-code guard."""
    call = node.exc
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "NotImplementedError"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value == "cannot reach"
    ):
        return False
    return True


def _is_closure_size_validation(node: ast.Raise) -> bool:
    """Return whether *node* validates an incompatible closure size."""
    call = node.exc
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "ValueError"
        and call.args
        and isinstance(call.args[0], ast.Constant)
    ):
        return False

    return call.args[0].value == (
        "code object received a closure with "
        "an unexpected number of free variables"
    )


def _raise_kind(node: ast.Raise) -> tuple[str, str] | None:
    """Classify one ``raise`` statement. Returns (kind, name) or None if irrelevant.

    ``kind`` is ``"operror"`` for ``OperationError(...)``/``oefmt(...)``,
    ``"raw"`` for a raw builtin exception in ``_RAW_EXCEPTION_NAMES``.
    """
    if node.exc is None:
        return None
    call = node.exc
    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)):
        return None
    name = call.func.id
    if name in ("OperationError", "oefmt"):
        return "operror", name
    if name in _RAW_EXCEPTION_NAMES:
        return "raw", name
    return None


def _check_file(path: Path, project_root: Path) -> list[dict]:
    result = parse_rpython_file(path)
    if result.parser != "ast" or result.ast_tree is None:
        return []

    tree = result.ast_tree
    rel = relative_to_root(path, project_root)
    findings: list[dict] = []

    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        operror_raises: list[ast.Raise] = []
        raw_raises: list[tuple[ast.Raise, str]] = []
        for sub in ast.walk(func):
            if isinstance(sub, ast.Raise):
                if (
                    _is_unreachable_not_implemented(sub)
                    or _is_closure_size_validation(sub)
                ):
                    continue

                classified = _raise_kind(sub)
                if classified is None:
                    continue
                kind, name = classified
                if kind == "operror":
                    operror_raises.append(sub)
                else:
                    raw_raises.append((sub, name))

        if not raw_raises:
            continue

        if operror_raises:
            # Same-function asymmetry: the strongest signal, per
            # pyframe.py's initialize_frame_scopes in the investigation.
            for raw_node, exc_name in raw_raises:
                findings.append(
                    _finding(
                        "interp-app-boundary-same-function-asymmetry",
                        "CONSIDER",
                        "high",
                        raw_node,
                        f"{func.name}() raises both OperationError/oefmt AND a raw "
                        f"{exc_name} -- same function uses both conventions, the "
                        f"strongest real signal found in the investigation behind "
                        f"this toolkit (pyframe.py's initialize_frame_scopes)",
                        f"raw exception: {exc_name}; correct-convention raises in "
                        f"same function: {len(operror_raises)}",
                    )
                )
        else:
            for raw_node, exc_name in raw_raises:
                findings.append(
                    _finding(
                        "interp-app-boundary-raw-exception",
                        "CONSIDER",
                        "low",
                        raw_node,
                        f"{func.name}() raises a raw {exc_name} with no OperationError/"
                        f"oefmt convention used elsewhere in the same function -- lower "
                        f"confidence than the same-function-asymmetry signal; caller "
                        f"reachability not yet checked here (see the caller-count pass "
                        f"in analyze())",
                        f"raw exception: {exc_name}",
                    )
                )

    for f in findings:
        f["file"] = rel
        f["function"] = f["message"].split("()", 1)[0].rsplit(" ", 1)[-1] if "()" in f["message"] else ""

    # W_* class resource-release pass (new in v0.1.1)
    findings.extend(_scan_w_class_resource_release(tree, rel))

    return findings


def _function_name_from_message(message: str) -> str:
    # "func_name() raises ..." -> "func_name"
    if "()" in message:
        return message.split("()", 1)[0].split()[-1]
    return ""


# ---------------------------------------------------------------------------
# W_* class resource-release pass
# ---------------------------------------------------------------------------

_RESOURCE_ACQUIRE_HINTS = frozenset({
    "open", "socket", "lock", "acquire", "malloc", "ffi.new",
})

_RELEASE_METHOD_NAMES = frozenset({
    "__del__", "descr_close", "close", "descr___del__",
})


def _rhs_call_name(node: ast.expr) -> str | None:
    """Return a simple name for the RHS call, or None if it's not a call."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        # e.g. ffi.new, os.open
        if isinstance(func.value, ast.Name):
            return f"{func.value.id}.{func.attr}"
        return func.attr
    return None


def _is_resource_acquisition(call_name: str) -> bool:
    """True if call_name contains any of the resource-acquisition hint words."""
    lower = call_name.lower()
    return any(hint in lower for hint in _RESOURCE_ACQUIRE_HINTS)


def _scan_w_class_resource_release(tree: ast.Module, rel: str) -> list[dict]:
    """Walk W_* classes; flag those that acquire resources with no release method."""
    findings: list[dict] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not node.name.startswith("W_"):
            continue

        # Collect method names present in the class body.
        method_names: set[str] = {
            item.name
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        has_release = bool(method_names & _RELEASE_METHOD_NAMES)
        if has_release:
            continue

        # Look for self.X = <resource-acquisition-call> in method bodies.
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for stmt in ast.walk(item):
                if not isinstance(stmt, ast.Assign):
                    continue
                # Check self.attr = <call>
                for target in stmt.targets:
                    if not (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                    ):
                        continue
                    call_name = _rhs_call_name(stmt.value)
                    if call_name and _is_resource_acquisition(call_name):
                        findings.append({
                            "type": "w-class-no-release",
                            "subtype": "W_CLASS_NO_RELEASE",
                            "classification": "REVIEW",
                            "confidence": "medium",
                            "line": node.lineno,
                            "file": rel,
                            "class_name": node.name,
                            "acquiring_method": item.name,
                            "resource_hint": call_name,
                            "message": (
                                f"{node.name}.{item.name}() assigns self.{target.attr} "
                                f"from '{call_name}' but the class has no "
                                f"__del__/descr_close/close/descr___del__ method"
                            ),
                            "detail": (
                                f"Reviewer must confirm resource is long-lived and not "
                                f"released through a wrapper or parent class"
                            ),
                        })
                        break  # one finding per method is enough

    return findings


_CALL_PATTERN_CACHE: dict[str, re.Pattern] = {}


def _count_callers(func_name: str, all_source_text: str) -> int:
    """Heuristic count of ``func_name(`` occurrences, excluding the definition itself.

    Text-based, not a real call-graph resolution -- see module docstring's
    caveats. ``def func_name(`` (the definition) is excluded from the count.
    """
    if func_name not in _CALL_PATTERN_CACHE:
        # (?<!\w) rejects a preceding word character (so "xfixedunpack("
        # doesn't match "fixedunpack") but deliberately allows a preceding
        # '.' -- most real interp-level calls are method calls
        # (args.fixedunpack(1)), and an earlier version of this regex
        # excluded '.' too, which meant it missed the single most common
        # real call shape entirely. Caught by a test, not by running
        # against real code this time -- worth keeping the test as the
        # regression guard either way.
        _CALL_PATTERN_CACHE[func_name] = re.compile(
            r"(?<!\w)" + re.escape(func_name) + r"\s*\(",
        )
    pattern = _CALL_PATTERN_CACHE[func_name]
    matches = pattern.findall(all_source_text)
    def_count = all_source_text.count(f"def {func_name}(")
    return max(0, len(matches) - def_count)


def analyze(target: str, *, max_files: int = 0) -> dict:
    resolved = Path(target).resolve()
    project_root = find_project_root(resolved)
    scan_root = resolved

    discovery = discover(target)

    if discovery.get("is_pypy_checkout"):
        checkout_root = Path(discovery["checkout_root"])
        files_by_layer = discovery.get("files_by_layer", {})
        # This checker's real surface (per the design doc's census) is
        # pypy/interpreter + pypy/module specifically, not the full
        # in-scope layer set -- narrower than the flagship/crown-jewel
        # scanners because OperationError/raw-exception conventions are
        # an interp-level concern, not something rlib/jit/gc code does.
        in_scope_rel_paths = [
            rel
            for layer in ("interpreter", "module")
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

    # Caller-reachability pass for the lower-confidence "raw-exception"
    # findings (not the same-function-asymmetry ones, which are already
    # high-confidence on their own signal). Build one combined text blob of
    # all in-scope non-test source once, rather than re-reading per finding.
    raw_findings = [f for f in findings if f["type"] == "interp-app-boundary-raw-exception"]
    if raw_findings:
        combined_source = "\n".join(
            parse_rpython_file(p).source for p in non_test_files if parse_rpython_file(p).source
        )
        for f in raw_findings:
            func_name = _function_name_from_message(f["message"])
            if not func_name:
                continue
            caller_count = _count_callers(func_name, combined_source)
            if caller_count == 0:
                f["classification"] = "ACCEPTABLE"
                f["confidence"] = "medium"
                f["detail"] += (
                    "; zero name-matched call sites found in non-test in-scope source "
                    "-- matches the fixedunpack pattern from the investigation (a raw "
                    "exception with no production callers resolved as intentional, "
                    "deliberately tested). Heuristic, not real call-graph analysis -- "
                    "verify before fully trusting an ACCEPTABLE here."
                )
            else:
                f["detail"] += f"; {caller_count} name-matched call site(s) found (heuristic count)"

    findings = deduplicate_findings(findings)
    return build_pypy_envelope(discovery, findings, functions_analyzed=len(non_test_files))


def main() -> None:
    target, max_files = parse_common_args(sys.argv[1:])
    result = analyze(target, max_files=max_files)
    emit(result)


if __name__ == "__main__":
    main()
