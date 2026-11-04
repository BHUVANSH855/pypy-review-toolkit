#!/usr/bin/env python3
"""Detect raw free()-pairing hazards and suspicious ffi attribute access.

The scanner checks selected patterns in lib_pypy/:

1. Uppercase ffi attribute access other than ffi.NULL. This is a heuristic
   intended to identify constant-like names that may be misspelled.
2. free() calls outside a syntactically enclosing self.lock block in classes
   that use self.lock elsewhere.

These are candidate signals, not confirmed defects. In particular, the
scanner does not resolve the CFFI API for a particular version, establish
runtime reachability, prove concurrent execution, or establish use-after-free.

Every finding requires manual source and call-path investigation.

Usage:
    python scan_free_pairing.py [path] [--max-files N]
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

# Minimal fallback used when the data file cannot be loaded.
_FALLBACK_FFI_ATTRS = frozenset({"NULL"})

# Path to the canonical cffi allowlist data file, relative to this script.
_ALLOWLIST_PATH = Path(__file__).resolve().parent.parent / "data" / "lib_pypy_cffi_allowlist.json"

# Loaded at module startup; _cffi_allowlist_warning is non-None if the file
# could not be loaded (surfaced in the scan envelope as a warning).
_cffi_allowlist_warning: str | None = None

try:
    with _ALLOWLIST_PATH.open(encoding="utf-8") as _f:
        _raw = json.load(_f)
    _KNOWN_FFI_ATTRS: frozenset[str] = frozenset(_raw["known_good_attributes"])
except FileNotFoundError:
    _KNOWN_FFI_ATTRS = _FALLBACK_FFI_ATTRS
    _cffi_allowlist_warning = (
        f"lib_pypy_cffi_allowlist.json not found at {_ALLOWLIST_PATH}; "
        "using minimal fallback set — Shape C detection may miss additional known-good attributes"
    )
except (KeyError, json.JSONDecodeError, OSError) as _exc:
    _KNOWN_FFI_ATTRS = _FALLBACK_FFI_ATTRS
    _cffi_allowlist_warning = (
        f"Failed to load lib_pypy_cffi_allowlist.json ({_exc}); "
        "using minimal fallback set — Shape C detection may miss additional known-good attributes"
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


def _check_suspicious_ffi_attrs(tree: ast.Module) -> list[tuple[ast.Attribute, str]]:
    """Flag `ffi.<UPPERCASE_NAME>` accesses where the name isn't a known-good cffi attribute.

    Strategy: flag any all-caps ffi attribute that is NOT in the loaded
    ``_KNOWN_FFI_ATTRS`` allowlist (sourced from lib_pypy_cffi_allowlist.json).
    All-caps names look like constants; cffi's one real constant in common use
    is ffi.NULL. Anything else that is all-caps is almost certainly a typo
    (like the confirmed bug ffi.NONE). Lowercase API members (ffi.cdef,
    ffi.set_source, ffi.compile, ffi.callback, …) are excluded from scanning
    entirely because the false-positive rate on them is too high without
    full cffi API version resolution.
    """
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "ffi":
            attr = node.attr
            # Only flag all-caps names that look like constants and are not
            # in the known-good set from lib_pypy_cffi_allowlist.json.
            if attr.isupper() and attr not in _KNOWN_FFI_ATTRS:
                found.append((node, attr))
    return found


def _is_free_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "free"


def _class_uses_lock(cls: ast.ClassDef) -> bool:
    for node in ast.walk(cls):
        if isinstance(node, ast.With):
            for item in node.items:
                ctx = item.context_expr
                if (
                    isinstance(ctx, ast.Attribute)
                    and ctx.attr == "lock"
                    and isinstance(ctx.value, ast.Name)
                    and ctx.value.id == "self"
                ):
                    return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("acquire", "release"):
            if (
                isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "lock"
                and isinstance(node.func.value.value, ast.Name)
                and node.func.value.value.id == "self"
            ):
                return True
    return False


def _method_free_calls_under_lock(method: ast.FunctionDef) -> tuple[list[ast.Call], list[ast.Call]]:
    """Split free() calls in *method* into (under_lock, not_under_lock)."""
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(method):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    under_lock: list[ast.Call] = []
    not_under_lock: list[ast.Call] = []
    for node in ast.walk(method):
        if not _is_free_call(node):
            continue
        cur = node
        in_lock = False
        while cur in parent:
            cur = parent[cur]
            if isinstance(cur, ast.With):
                for item in cur.items:
                    ctx = item.context_expr
                    if (
                        isinstance(ctx, ast.Attribute)
                        and ctx.attr == "lock"
                        and isinstance(ctx.value, ast.Name)
                        and ctx.value.id == "self"
                    ):
                        in_lock = True
            if cur is method:
                break
        if in_lock:
            under_lock.append(node)
        else:
            not_under_lock.append(node)
    return under_lock, not_under_lock


def _check_file(path: Path, project_root: Path) -> list[dict]:
    result = parse_rpython_file(path)
    if result.parser != "ast" or result.ast_tree is None:
        return []

    tree = result.ast_tree
    rel = relative_to_root(path, project_root)
    findings: list[dict] = []

    for node, attr in _check_suspicious_ffi_attrs(tree):
        findings.append(
            _finding(
                "suspicious-ffi-attribute",
                "CONSIDER",
                "medium",
                node,
                f"ffi.{attr} is an uppercase ffi attribute other than ffi.NULL; "
                f"verify the attribute against the CFFI API version and inspect its "
                f"execution path before classifying it as a defect",
                "verify this is a real cffi API member for the version in use, not a typo",
            )
        )

    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        if not _class_uses_lock(cls):
            continue
        for method in cls.body:
            if not isinstance(method, ast.FunctionDef):
                continue
            _, not_under_lock = _method_free_calls_under_lock(method)
            for call in not_under_lock:
                findings.append(
                    _finding(
                        "unlocked-free-call",
                        "CONSIDER",
                        "medium",
                        call,
                        f"{cls.name}.{method.name}() calls free() outside a syntactically "
                        f"enclosing self.lock block, while the class uses self.lock elsewhere; "
                        f"inspect concurrency, ownership, and all callers to determine whether "
                        f"the operation requires synchronization",
                        f"class: {cls.name}",
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
        in_scope_rel_paths = list(files_by_layer.get("lib_pypy", []))
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
    envelope = build_pypy_envelope(discovery, findings, functions_analyzed=len(non_test_files))
    if _cffi_allowlist_warning:
        envelope.setdefault("warnings", []).append(_cffi_allowlist_warning)
    return envelope


def main() -> None:
    target, max_files = parse_common_args(sys.argv[1:])
    result = analyze(target, max_files=max_files)
    emit(result)


if __name__ == "__main__":
    main()
