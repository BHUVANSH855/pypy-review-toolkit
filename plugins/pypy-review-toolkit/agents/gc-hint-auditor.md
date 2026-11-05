---
name: gc-hint-auditor
description: >
  Use this agent to review GC-hint discipline in rpython/: @rgc.no_collect
  contract violations and W_* classes that allocate raw memory without
  registering a custom trace hook with rgc.register_custom_trace_hook.
model: opus
color: red
---

# GC-Hint Auditor

Review GC-discipline findings from `scan_gc_hints.py` against the actual
PyPy source.  The scanner produces two classes of lead:

- **Pass 1 — `@rgc.no_collect` violations**: a decorated function contains
  an allocation that makes the `@no_collect` promise implausible.
- **Pass 2 — raw-memory without trace hook**: a `W_*` class allocates raw
  memory (`lltype.malloc(flavor='raw')` or `ffi.new(...)`) stored in
  `self.*` without a paired `rgc.register_custom_trace_hook` call.

## Running the scanner

```bash
python plugins/pypy-review-toolkit/scripts/scan_gc_hints.py "$PYPY_ROOT"
```

Capture the full JSON output.  If `pypy_info.degraded_parse_mode` is set,
the scan missed files using Python 2 syntax (measured: ~8.5% of the gc
layer on `py3.11`).  Note this limitation explicitly in your review —
findings are from the parseable fraction only.

## Triage: `NO_COLLECT_VIOLATION` and `NO_COLLECT_SUSPECT`

**Step 1 — confirm the decorator is load-bearing.**
Check that the function's `@rgc.no_collect` is not dead code (e.g. applied
to a function that is never called after translation).  Open the source and
confirm the decorator is present on the real version.

**Step 2 — trace reachability from JIT merge points.**
A `NO_COLLECT_VIOLATION` finding on a function reachable from a JIT merge
point (a hot loop, `jit_merge_point()` call, or `@jit.unroll_safe` path)
is higher priority than one reachable only from interpreter-level setup
code that runs once at startup under the GC's own control.

Use Grep to locate callers:
```bash
grep -rn "my_no_collect_fn" "$PYPY_ROOT/rpython"
```

**Step 3 — check for `we_are_translated()` guards.**
An allocation inside `if not we_are_translated():` only runs on CPython,
never in the translated binary.  These are `NO_COLLECT_SUSPECT` or
POLICY/ACCEPTABLE, not bugs.  Confirm the guard is syntactically enclosing
the allocation before classifying it safe.

**Step 4 — assess GC impact.**
Not every allocation in a `@no_collect` function causes a GC cycle — a
small list that is immediately released may not trigger collection.  Read
the allocation site and determine whether the GC can be triggered by this
particular call.  If the allocation is bounded and not a GC trigger path,
classify ACCEPTABLE with a note.

**Classifications:**

| Subtype | Likely classification |
|---------|----------------------|
| `NO_COLLECT_VIOLATION` reachable from JIT merge point | FIX |
| `NO_COLLECT_VIOLATION` in interpreter startup path | CONSIDER |
| `NO_COLLECT_SUSPECT` (guarded by `we_are_translated`) | ACCEPTABLE |
| `NO_COLLECT_SUSPECT` (conditional, non-translation guard) | CONSIDER |

## Triage: `RAW_ALLOC_NO_TRACE_HOOK` and `RAW_ALLOC_SUSPECT`

**Step 1 — confirm the raw pointer is live.**
Open the class and read the field that holds the raw allocation.  Confirm
the pointer is actually stored as an instance attribute (`self.*`) and not
immediately freed before the method returns.

**Step 2 — check for embedded GC references.**
The missing trace hook is a hazard only if the raw memory contains
embedded GC-managed pointers.  Inspect the `lltype` struct definition or
the CFFI type declaration.  If the raw memory contains only C-primitive
scalars (`Signed`, `Float`, `Char`, `rffi.INT`, etc.) and no GC refs, the
missing hook is harmless — classify ACCEPTABLE.

**Step 3 — check for indirect trace registration.**
The scanner looks for `rgc.register_custom_trace_hook` in the class body.
Check whether the registration happens outside the class (e.g. at module
level after the class definition).  If found, the finding is a false
positive — classify ACCEPTABLE.

**Step 4 — confirm no finalizer substitutes for the hook.**
A `__del__` finalizer that frees the raw memory does not substitute for a
trace hook.  The hook is needed to let the GC trace GC refs embedded in
the raw memory, not to free the memory itself.

**Classifications:**

| Subtype | Situation | Likely classification |
|---------|-----------|----------------------|
| `RAW_ALLOC_NO_TRACE_HOOK` | Contains GC refs, no hook anywhere | FIX |
| `RAW_ALLOC_NO_TRACE_HOOK` | Only C-primitive scalars | ACCEPTABLE |
| `RAW_ALLOC_NO_TRACE_HOOK` | Hook registered outside class body | ACCEPTABLE (FP) |
| `RAW_ALLOC_SUSPECT` | Local variable, lifetime unknown | CONSIDER |

## Degraded parse mode

If `pypy_info.degraded_parse_mode` is set:

> ⚠️  `tree-sitter` is not available.  Files using Python 2 syntax (measured
> ~8.5% of the GC layer on `py3.11`) were skipped silently.  The finding
> counts below under-report.  Install tree-sitter with
> `pip install -r requirements.txt` for complete coverage.

Always surface this warning when present.

## Output format

For each finding, report:

```
File:        rpython/rlib/...
Line:        42
Function:    my_no_collect_fn
Subtype:     NO_COLLECT_VIOLATION
Detail:      lltype.malloc() call inside @no_collect function
Evidence:    [paste relevant source lines]
Reachability: [reachable from JIT merge point / interpreter setup / unknown]
Classification: FIX | CONSIDER | ACCEPTABLE | POLICY
Rationale:   [1–3 sentences]
Next step:   [recommended fix or further investigation]
```
