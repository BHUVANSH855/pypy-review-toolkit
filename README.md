# pypy-review-toolkit

A Claude Code plugin for statically reviewing PyPy's own RPython-level
implementation — the interpreter core, object space, and JIT — for
correctness bugs and translation hazards specific to how PyPy is built.

Sibling of [cpython-review-toolkit](https://github.com/devdanzin/cpython-review-toolkit)
and [rustpy-review-toolkit](https://github.com/devdanzin/rustpy-review-toolkit).
Reviews PyPy's own source, not code that runs on PyPy, and not the
CPython-compatible stdlib PyPy ships in `lib-python/`/`lib_pypy/`.

Full design rationale, including six rounds of empirical investigation
against a real PyPy checkout, is in `pypy-review-toolkit-design.md`.

## Status: v0.1, all four scanners built and validated against the real checkout

**Scanners** (`plugins/pypy-review-toolkit/scripts/`):

| Scanner | Role | Verified against real checkout |
|---|---|---|
| `scan_translated_divergence.py` | Flagship (working hypothesis) — `we_are_translated()` arm divergence | 281 findings across 2002 files; two real bugs found and fixed during validation (scope leak into `lib-python/`, and the single-arm suppress rule's real coverage corrected from a claimed 79% down to a measured ~22%) |
| `scan_immutability_contracts.py` | Crown jewel — `_immutable_fields_`/`@jit.elidable` mismatches | Finds exactly the 7 real candidates identified during investigation, correctly tiered; two real bugs found and fixed (`jit.promote()` proximity needed to be class-wide, not method-local; the promoted argument needed to be checked as literally `self`) |
| `scan_interp_app_boundary.py` | `OperationError` leaks, leading with same-function-asymmetry | 96 findings; correctly resolves `fixedunpack` as ACCEPTABLE via a caller-reachability heuristic (one real regex bug found and fixed along the way) |
| `scan_rpython_restrictions.py` | RPython-restriction violations, honestly narrow scope | 70 `**kwargs` findings; `eval`/`exec` deliberately excluded (0 of 21 real candidates were genuine violations, all traced individually) |

**Infrastructure:**
- `discover_pypy.py` — checkout detection, layer classification
  (rlib/jit/gc/interpreter/objspace/module in scope; annotator/rtyper/
  translator explicitly named out of scope), review-slice manifest
- `pypy_utils.py` — `ast`-first, tree-sitter-python-fallback parsing
  dispatch (17.4% of real files fail `ast.parse()` on 2.7-only syntax;
  tree-sitter-python recovers all of them), plus RPython semantic helpers
- Three chassis scripts (`scan_common.py`, `analyze_history.py`,
  `measure_complexity.py`) vendored verbatim from
  [code-review-toolkit](https://github.com/devdanzin/code-review-toolkit)

**Agents** (`plugins/pypy-review-toolkit/agents/`): `translated-divergence-auditor`,
`immutability-contract-auditor`, `interp-app-boundary-checker`,
`rpython-restriction-scanner`, `jit-trace-reviewer` (qualitative, no
backing script), `git-history-analyzer`.

**Commands** (`plugins/pypy-review-toolkit/commands/`): `explore`, `health`,
`hotspots`, `known-issues`.

**Data** (`plugins/pypy-review-toolkit/data/`): `pypy_non_bugs.md` (seeded
with every real false-positive pattern found during validation),
`jit_hint_contracts.json`, `pypy_known_bugs.tsv` (empty schema, pending
fusil-on-PyPy).

**35 tests passing**, including regression fixtures for every real bug
found while validating each scanner against the actual checkout.

## Requirements

```
pip install -r requirements.txt
```

**tree-sitter is required, not optional.** PyPy's RPython source is written in
Python 2 syntax on *every* branch -- the py3.x branches implement Python 3, they
are not written in it -- so a meaningful share of the tree fails
`ast.parse()` under a Python 3 host. Without `tree_sitter` +
`tree_sitter_python` installed, `pypy_utils.py` has no fallback parser and those
files are skipped **silently**: scanners return a clean, confident, materially
incomplete result.

Measured on a real checkout at `py3.11` (`fe2af5843a`), share of `.py` files
failing `ast.parse()` under CPython 3.14:

| layer | files | ast.parse() failures |
|---|---|---|
| `pypy/interpreter` | 124 | **29.0%** |
| `rpython/rlib` | 253 | **26.1%** |
| `pypy/objspace` | 108 | **25.9%** |
| `rpython/memory` | 62 | 16.1% |
| `pypy/module` | 820 | 14.8% |
| `lib_pypy` | 189 | 10.6% |
| `rpython/jit` | 635 | 8.5% |

When tree-sitter is missing, every scanner's envelope now carries a
`degraded_parse_mode` warning in `pypy_info` saying so, rather than leaving the
gap to be inferred from a `"tree_sitter_available": false` flag.

## Install

Not yet published to a marketplace. For local development:

```
claude --plugin-dir plugins/pypy-review-toolkit
```
