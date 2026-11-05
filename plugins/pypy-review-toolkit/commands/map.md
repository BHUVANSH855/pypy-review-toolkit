---
name: map
description: "Build a coverage map of all scanners, their layers, and known reachability entry points"
argument-hint: "[pypy-checkout-path]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task"]
---

# PyPy Review Toolkit — Map

Produces a structured coverage map showing which layers each scanner covers,
how many files are in scope, and which functions are known to be reachable from
user Python code (high-priority review targets).

**Scope:** "$ARGUMENTS" (default: current directory, expected to be a PyPy checkout).

## Steps

### Step 1 — Discovery

Run `discover_pypy.py` on the target to enumerate layers, file counts, and
confirm this is a recognised PyPy checkout.

```bash
python plugins/pypy-review-toolkit/scripts/discover_pypy.py "$ARGUMENTS"
```

Record `files_by_layer` and `checkout_root` from the output.

### Step 2 — Scanner layer inventory

For each scanner in `plugins/pypy-review-toolkit/scripts/scan_*.py`, read its
opening `# covers:` comment (added in v0.1.1) to determine which layers it
targets. Produce a table:

| Scanner | Layers covered | Findings produced |
|---------|---------------|-------------------|
| `scan_translated_divergence` | interpreter, objspace, module, jit, rlib, gc | TRANSLATED_DIVERGENCE |
| `scan_immutability_contracts` | interpreter, objspace, jit, rlib | IMMUTABLE_FIELD_MUTATED, JIT_ELIDABLE_SIDE_EFFECT, … |
| `scan_interp_app_boundary` | interpreter, module, objspace | INTERP_APP_BOUNDARY_*, W_CLASS_NO_RELEASE |
| `scan_rpython_restrictions` | all in-scope layers | RPYTHON_UNBOUNDED_KWARGS, RPYTHON_MIXED_CONTAINER, RPYTHON_GENERATOR_EXPR |
| `scan_unvalidated_helper_calls` | module, interpreter, objspace | UNVALIDATED_HELPER_CALL_* |
| `scan_sibling_guard_consistency` | interpreter, objspace, module | SIBLING_GUARD_* |
| `scan_cross_class_method_guard` | interpreter, objspace, module | CROSS_CLASS_GUARD_MISSING |
| `scan_free_pairing` | lib_pypy | FREE_PAIRING_*, FFI_NULL_TYPO |
| `scan_gc_hints` | interpreter, rlib, gc | GC_HINT_MISSING, NO_COLLECT_VIOLATION |
| `scan_jit_driver` | jit, interpreter | JITDRIVER_ARG_MISMATCH, UNROLL_SAFE_UNBOUNDED, DEAD_JITDRIVER |

### Step 3 — Reachability entry points

Read `plugins/pypy-review-toolkit/data/rpython_reachability_sources.json`.
List every entry with `"confirmed": true`, grouped by `entry_via`:

- **interp2app** — directly callable from user Python
- **space.w_*** — instantiated by user code as a Python object
- **bytecode-dispatch** — invoked on every matching bytecode

These are the highest-priority review targets: a bug here is reachable by any
user Python program without special knowledge.

### Step 4 — Gap analysis

Cross-reference the scanner table (Step 2) with the reachability map (Step 3).
Identify functions or classes in the reachability map whose layer is covered by
at least one scanner but which have zero findings flagged yet. Report these as
**unchecked entry points** — they warrant manual review or future scanner
extensions.

Summarise the map as:

```
Coverage map — <checkout_root> (<commit>)
  Layers in scope: <N>
  Files in scope:  <N>
  Scanners active: <N>
  Reachability entry points: <N> confirmed
  Unchecked entry points:    <N>
```
