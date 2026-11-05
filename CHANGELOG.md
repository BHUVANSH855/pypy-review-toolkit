# Changelog

## v0.1.1 — 2026-11-05

### Added
- `/map` command: builds a coverage table of all scanners, their target PyPy
  layers, and known `interp2app` reachability entry points; outputs a gap
  analysis of unchecked surfaces.
- `/informed-explore` command: same phased review as `/explore` but runs
  `scan_unvalidated_helper_calls.py` with `--reachability-map` so findings
  are annotated `HIGH`/`UNKNOWN` before agent triage.
- `scripts/run_differential.py`: diffs two scanner JSON envelopes keyed by
  `(file, type, line)`; returns `new`, `resolved`, `unchanged_count`.
- `data/rpython_reachability_sources.json`: 20 confirmed `interp2app` entry
  points from `pypy/objspace/std/` (py3.11 branch, `fe2af5843a`).
- `--reachability-map` flag on `scan_unvalidated_helper_calls.py`: annotates
  findings with `reachability: "HIGH"` or `"UNKNOWN"`.
- `# covers: <layers>` annotation on all 9 scanner headers for `/map`.
- 8 new tests (5 differential, 3 reachability); 211 tests total.

### Changed
- `plugin.json` bumped to `0.1.1`; `/map` and `/informed-explore` registered.

---

## v0.1.0 — 2026-11-05

First public release. Covers issues #13–#20 of the v0.1.0 milestone.

### Scanners (9 total)
- `scan_translated_divergence.py` — `we_are_translated()` arm divergence
  (flagship; 295 findings on py3.11 `fe2af5843a`).
- `scan_immutability_contracts.py` — `_immutable_fields_` / `@jit.elidable`
  mismatches (crown jewel; `@jit.unroll_safe` unbounded-loop pass added in
  this release).
- `scan_interp_app_boundary.py` — `OperationError` leaks at the interp/app
  boundary; `W_CLASS_NO_RELEASE` pass added in this release.
- `scan_rpython_restrictions.py` — RPython restriction violations;
  `RPYTHON_MIXED_CONTAINER` and `RPYTHON_GENERATOR_EXPR` checks added in
  this release.
- `scan_unvalidated_helper_calls.py` — user value into RPython helper without
  precondition check; clause-splitting separates range and surrogate
  obligations independently (catches the newformat.py:847 shape).
- `scan_sibling_guard_consistency.py` — app-exposed method missing the guard
  convention its class establishes.
- `scan_cross_class_method_guard.py` — same method name on sibling classes;
  guard present on some, missing on others.
- `scan_free_pairing.py` — raw `free()` pairing and suspicious `ffi.*` access
  in `lib_pypy/`.
- `scan_gc_hints.py` — `@rgc.must_be_light_finalizer` / `@rgc.no_collect`
  discipline and unchecked `rffi.raw_malloc` allocations.
- `scan_jit_driver.py` — `JitDriver` greenkey / `get_printable_location`
  contract; dead-driver detection.

### Agents (11 total)
`translated-divergence-auditor`, `immutability-contract-auditor`,
`interp-app-boundary-checker`, `rpython-restriction-scanner`,
`unvalidated-helper-call-auditor`, `jit-trace-reviewer`,
`git-history-analyzer`, `lib-pypy-cffi-auditor`, `sibling-guard-auditor`,
`cross-class-guard-auditor`, `gc-hint-auditor`.

### Commands (6 total)
`explore`, `health`, `hotspots`, `known-issues`, `map`, `informed-explore`.

### Data
- `sensitive_rpython_helpers.json`: 34 audited RPython helper entries.
- `rpython_reachability_sources.json`: 20 confirmed `interp2app` entry points.
- `review_slices.json`: real PyPy py3.11 checkout slice data.
- `rpython_known_patterns.json`: curated RPython pattern catalogue.
- `lib_pypy_cffi_allowlist.json`: externalized CFFI allowlist.

### Out of scope for v0.1.0
- Full transitive call-graph reachability (reachability data is manually
  seeded; `UNKNOWN` does not mean unreachable).
- Automatic regeneration of `review_slices.json` in CI.
- Annotator, rtyper, and translator layers.
- `lib-python/` compatibility stdlib.

### Tests
211 passing.
