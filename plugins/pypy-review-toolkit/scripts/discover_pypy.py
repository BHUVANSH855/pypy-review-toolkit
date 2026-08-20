#!/usr/bin/env python3
"""Detect a PyPy checkout and resolve its review profile.

PyPy (https://github.com/pypy/pypy) is a Python interpreter implemented in
RPython -- a restricted, statically-annotated subset of Python 2 syntax, not
a separate language. This toolkit reviews *PyPy's own implementation* -- the
RPython interpreter core, object space, and JIT -- not code that runs on
PyPy, and not the CPython-compatible stdlib PyPy ships in
``lib-python/``/``lib_pypy/``.

Detection cascade (first match wins):
  1. A directory containing both ``rpython/`` and ``pypy/interpreter/`` --
     a full PyPy checkout, the canonical case.
  2. A path whose ancestry contains a directory with both of the above --
     a scoped review of a sub-checkout or worktree.
  3. Fallback: the target itself, reported but flagged as unrecognized.

Layer classification (design doc §2.4/§3.6) matters because the bug
taxonomy differs by directory:

  rlib               -- GC hints, JIT hints, we_are_translated() branches
  jit                -- elidable/promote/unroll contracts, trace-time code
  gc                 -- barrier discipline, moving-GC assumptions
  interpreter         -- OperationError vs raw exceptions, bytecode dispatch
  objspace           -- W_*Object invariants, unwrap_spec/interp2app boundary
  module             -- same concerns as interpreter/objspace (in scope per
                         design doc §3.6 -- it was silently missing from the
                         original scope despite being the single largest
                         directory in pypy/, and real candidates were found
                         there without deliberately targeting it)
  annotator/rtyper/translator -- explicitly OUT OF SCOPE (meta-level tooling
                         that operates on RPython source during translation,
                         not translated runtime code itself) -- named here,
                         not silently skipped, per design doc §5's
                         classification discipline

Also generates the review-slice manifest (``data/review_slices.json``) --
necessary from v0.1 given the real in-scope surface is ~687K-867K lines,
roughly 2-2.4x cpython-review-toolkit's ~358K-line surface, which already
needed 37 pre-partitioned slices at that smaller scale.

Usage:
    python discover_pypy.py [path]
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_common import discover_python_files, find_project_root  # noqa: E402

try:
    from pypy_utils import _TS_AVAILABLE
except ImportError:  # pragma: no cover
    _TS_AVAILABLE = False


# Directory (relative to checkout root) -> layer name. Longest-prefix match
# wins, so more specific paths (e.g. "rpython/memory/gc") are listed before
# their broader parents where order matters -- see _classify_layer.
_LAYER_ROLES: dict[str, str] = {
    "rpython/annotator": "out-of-scope-annotator",
    "rpython/rtyper": "out-of-scope-rtyper",
    "rpython/translator": "out-of-scope-translator",
    "rpython/memory/gc": "gc",
    "rpython/memory": "gc",
    "rpython/jit": "jit",
    "rpython/rlib": "rlib",
    "pypy/interpreter": "interpreter",
    "pypy/objspace": "objspace",
    "pypy/module": "module",
    "lib_pypy": "lib_pypy",
}

# Layers considered in-scope for v0.1 review (design doc §3.6: pypy/module
# is IN scope, not deferred -- real candidates were already found there).
# lib_pypy/ added per design doc §9.3/§0.1 Shape B -- PyPy's hand-written
# cffi-based stdlib replacements (confirmed real: lib_pypy/gdbm.py and
# lib_pypy/_pypy_util_cffi.py both call free(), matching danzin's
# confirmed bug #4's shape). Distinct from lib-python/, the CPython-
# compatible stdlib PyPy ships, which remains explicitly out of scope.
IN_SCOPE_LAYERS = frozenset({"rlib", "jit", "gc", "interpreter", "objspace", "module", "lib_pypy"})

# Rough target lines per review slice. cpython-review-toolkit measured that
# a ~39,800-line slice strained a single review pass while ~13,250 lines
# triaged well -- using the same target here.
_SLICE_TARGET_LINES = 13_000


def _classify_layer(rel_path: str) -> str:
    """Classify *rel_path* (POSIX-style, relative to checkout root) into a layer.

    Longest matching prefix wins so "rpython/memory/gc" is distinguished
    from the broader "rpython/memory" (both currently map to "gc", but kept
    separate here in case memory/gctransform or other memory/ subdirs need
    their own role later).
    """
    best_match = ""
    best_role = "unclassified"
    for prefix, role in _LAYER_ROLES.items():
        if rel_path == prefix or rel_path.startswith(prefix + "/"):
            if len(prefix) > len(best_match):
                best_match = prefix
                best_role = role
    return best_role


def _is_pypy_checkout(path: Path) -> bool:
    return (path / "rpython").is_dir() and (path / "pypy" / "interpreter").is_dir()


def _find_checkout_root(start: Path) -> Path | None:
    if _is_pypy_checkout(start):
        return start
    current = start
    while current != current.parent:
        if _is_pypy_checkout(current):
            return current
        current = current.parent
    return None


def _is_shallow_clone(root: Path) -> bool:
    return (root / ".git" / "shallow").exists()


def _git_branch_and_commit(root: Path) -> tuple[str | None, str | None]:
    """Best-effort current branch name and short commit hash via `git`.

    Added per design doc §0.2/§9.4: trying to verify two of danzin's
    confirmed bugs against this toolkit's own checkout surfaced a real
    problem -- `lib_pypy/_lzma.py` and `_dealloc_warn_w` don't exist in the
    `main` branch as cloned, which targets a different/newer Python version
    than the PyPy 7.3.23/3.11.15 release actually being fuzzed. Every
    report this toolkit produces should say what it was actually run
    against, so a mismatch like that is visible immediately rather than
    discovered by hand days later.
    """
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        branch_name = branch.stdout.strip() if branch.returncode == 0 else None
        commit_hash = commit.stdout.strip() if commit.returncode == 0 else None
        return branch_name or None, commit_hash or None
    except (OSError, subprocess.SubprocessError):
        return None, None


def _pypy_version_string(root: Path) -> str | None:
    """Best-effort PyPy version string from pypy/module/sys/version.py.

    That file defines version numbers as literals rather than a single
    importable string, so this is a coarse grep, not a real parse -- good
    enough to flag an obvious mismatch, not a substitute for actually checking
    out the right tag/branch per §9.4.

    Returns ``"<cpython-version> [PyPy <pypy-version>]"`` when both literals are
    present, the CPython tuple alone when only that one is, else ``None``.

    The path matters: ``pypy/tool/version.py`` (previously read here) does not
    exist in any PyPy checkout -- ``CPYTHON_VERSION``/``PYPY_VERSION`` live in
    ``pypy/module/sys/version.py``. Reading the wrong path made this return
    ``None`` unconditionally, which silently disabled the one field §9.4 added
    to catch a branch mismatch. A review of ``main`` (Python 2.7.18) mistaken
    for the 3.11 line is exactly the failure this is meant to prevent.
    """
    version_file = root / "pypy" / "module" / "sys" / "version.py"
    if not version_file.is_file():
        return None
    try:
        text = version_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    cpython = re.search(r"^CPYTHON_VERSION\s*=\s*\(([^)]+)\)", text, re.MULTILINE)
    if not cpython:
        return None
    hint = cpython.group(1).replace(" ", "")
    pypy = re.search(r"^PYPY_VERSION\s*=\s*\(([^)]+)\)", text, re.MULTILINE)
    if pypy:
        hint = f"{hint} [PyPy {pypy.group(1).replace(' ', '')}]"
    return hint


def discover(target: str) -> dict:
    """Detect the PyPy checkout at or above *target* and classify its files.

    Returns a dict with checkout metadata, per-file layer classification,
    and per-layer file/line totals. Does not itself generate the review-slice
    manifest -- see ``build_review_slices``, called separately so scanners
    that only need discovery metadata aren't forced to pay for a full file
    walk.
    """
    resolved = Path(target).resolve()
    scan_root = resolved if resolved.is_dir() else resolved.parent
    checkout_root = _find_checkout_root(scan_root)
    project_root = find_project_root(scan_root)

    if checkout_root is None:
        return {
            "project_root": str(project_root),
            "scan_root": str(scan_root),
            "is_pypy_checkout": False,
            "checkout_root": None,
        }

    files_by_layer: dict[str, list[str]] = {}
    lines_by_layer: dict[str, int] = {}
    for path in discover_python_files(scan_root):
        try:
            rel = path.relative_to(checkout_root).as_posix()
        except ValueError:
            continue
        layer = _classify_layer(rel)
        files_by_layer.setdefault(layer, []).append(rel)
        try:
            n_lines = sum(1 for _ in path.open("rb"))
        except OSError:
            n_lines = 0
        lines_by_layer[layer] = lines_by_layer.get(layer, 0) + n_lines

    branch_name, commit_hash = _git_branch_and_commit(checkout_root)

    return {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "checkout_root": str(checkout_root),
        "is_pypy_checkout": True,
        "is_shallow_clone": _is_shallow_clone(checkout_root),
        "git_branch": branch_name,
        "git_commit": commit_hash,
        "pypy_cpython_version_hint": _pypy_version_string(checkout_root),
        "tree_sitter_available": _TS_AVAILABLE,
        "in_scope_layers": sorted(IN_SCOPE_LAYERS),
        "files_by_layer": {k: sorted(v) for k, v in files_by_layer.items()},
        "file_counts_by_layer": {k: len(v) for k, v in files_by_layer.items()},
        "lines_by_layer": lines_by_layer,
    }


def build_review_slices(discovery: dict) -> dict:
    """Partition in-scope files into review slices of roughly equal size.

    Mirrors cpython-review-toolkit's review-slice partitioning: pack files
    (largest-first, per layer, so a slice doesn't straddle unrelated layers
    unless a layer alone is smaller than the target) into slices near
    ``_SLICE_TARGET_LINES``. Line counts here come from ``discovery``, which
    counts raw lines via file line count, not the (slower) full AST/complexity
    analysis cpython-review-toolkit's own slicing tool uses -- adequate for
    partitioning, not for reporting exact reviewable-line totals.
    """
    checkout_root = discovery.get("checkout_root")
    if not checkout_root:
        return {"slices": [], "error": "not a PyPy checkout"}

    root = Path(checkout_root)
    files_by_layer = discovery.get("files_by_layer", {})

    slices: list[dict] = []
    for layer in sorted(IN_SCOPE_LAYERS):
        rel_paths = files_by_layer.get(layer, [])
        if not rel_paths:
            continue
        current_slice: list[str] = []
        current_lines = 0
        for rel in rel_paths:
            try:
                n_lines = sum(1 for _ in (root / rel).open("rb"))
            except OSError:
                n_lines = 0
            if current_slice and current_lines + n_lines > _SLICE_TARGET_LINES:
                slices.append(
                    {
                        "layer": layer,
                        "files": current_slice,
                        "approx_lines": current_lines,
                    }
                )
                current_slice = []
                current_lines = 0
            current_slice.append(rel)
            current_lines += n_lines
        if current_slice:
            slices.append(
                {
                    "layer": layer,
                    "files": current_slice,
                    "approx_lines": current_lines,
                }
            )

    for i, s in enumerate(slices):
        s["slice_id"] = f"{s['layer']}-{i:03d}"

    return {"slices": slices, "slice_count": len(slices), "target_lines_per_slice": _SLICE_TARGET_LINES}


def build_pypy_envelope(
    discovery: dict,
    findings: list[dict],
    *,
    functions_analyzed: int = 0,
) -> dict:
    """Assemble a PyPy-flavoured report envelope.

    Mirrors ``scan_common.build_envelope`` (kept verbatim, never forked) but
    projects PyPy/RPython metadata into ``pypy_info`` instead of the generic
    Python-project shape -- layer classification, tree-sitter availability,
    and scope information that's meaningless for an arbitrary Python project
    but load-bearing here. ``findings`` should already be deduplicated by the
    caller (``scan_common.deduplicate_findings``); summary counts are derived
    here, matching the family's envelope convention (design doc §5).
    """
    pypy_info = {
        "is_pypy_checkout": discovery.get("is_pypy_checkout", False),
        "checkout_root": discovery.get("checkout_root"),
        "is_shallow_clone": discovery.get("is_shallow_clone", False),
        "git_branch": discovery.get("git_branch"),
        "git_commit": discovery.get("git_commit"),
        "pypy_cpython_version_hint": discovery.get("pypy_cpython_version_hint"),
        "tree_sitter_available": discovery.get("tree_sitter_available", False),
        "in_scope_layers": discovery.get("in_scope_layers", []),
        "file_counts_by_layer": discovery.get("file_counts_by_layer", {}),
        "lines_by_layer": discovery.get("lines_by_layer", {}),
    }
    by_type: dict[str, int] = {}
    by_classification: dict[str, int] = {}
    for finding in findings:
        ftype = str(finding.get("type", ""))
        cls = str(finding.get("classification", ""))
        by_type[ftype] = by_type.get(ftype, 0) + 1
        by_classification[cls] = by_classification.get(cls, 0) + 1
    return {
        "project_root": discovery.get("project_root", ""),
        "scan_root": discovery.get("scan_root", ""),
        "pypy_info": pypy_info,
        "functions_analyzed": functions_analyzed,
        "findings": findings,
        "summary": {"by_type": by_type, "by_classification": by_classification},
    }


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    discovery = discover(target)

    if "--slices" in sys.argv:
        discovery["review_slices"] = build_review_slices(discovery)

        script_root = Path(__file__).resolve().parent
        toolkit_root = script_root.parent.parent.parent
        manifest_path = (
            toolkit_root
            / "plugins"
            / "pypy-review-toolkit"
            / "data"
            / "review_slices.json"
        )
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(discovery, indent=2, default=str) + "\n",
            encoding="utf-8",
        )

    json.dump(discovery, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
