# pypy-review-toolkit

A Claude Code plugin for statically reviewing PyPy's own RPython-level
implementation — the interpreter core, object space, and JIT — for
correctness bugs and translation hazards specific to how PyPy is built.

Sibling of [cpython-review-toolkit](https://github.com/devdanzin/cpython-review-toolkit)
and [rustpy-review-toolkit](https://github.com/devdanzin/rustpy-review-toolkit).
Reviews PyPy's own source, not code that runs on PyPy, and not the
CPython-compatible stdlib PyPy ships in `lib-python/`/`lib_pypy/`.

## Findings

Bugs reproduced on the interpreter are catalogued in
[devdanzin/pypy-review-findings](https://github.com/devdanzin/pypy-review-findings)
(15 records — 12 reproduced, 3 static-confirmed), each with a minimal
reproducer, captured output from both PyPy and CPython, and a root-cause
analysis. `catalog/known_bugs.tsv` there is generated in the schema this
toolkit's `known-issues` command consumes.

Highlights:
- **PYPYR-0012** — `re.Pattern('x', 0, [7, 2**32-1, 0]).match('abc')` →
  SIGSEGV from three lines of stdlib Python
  ([pypy/pypy#5571](https://github.com/pypy/pypy/issues/5571))
- **PYPYR-0014** — raw `ValueError` in `pyframe.py` escapes as `SystemError`;
  first confirmed instance of the interp/app boundary bug class
- **PYPYR-0004, 0005, 0009, 0011** — four instances of the two-clause
  precondition shape: surrogate codepoints leak
  `SystemError: unexpected internal exception (please report a bug)`

## Status: v0.1

**Scanners** (`plugins/pypy-review-toolkit/scripts/`):

| Scanner | Role | Signal |
|---|---|---|
| `scan_translated_divergence.py` | `we_are_translated()` arm divergence (flagship, working hypothesis) | 295 findings on `py3.11` |
| `scan_immutability_contracts.py` | `_immutable_fields_`/`@jit.elidable` mismatches (crown jewel) | 7 real candidates, all individually traced |
| `scan_interp_app_boundary.py` | `OperationError` leaks; leads with same-function-asymmetry signal | `pyframe.py:236` flagged; `fixedunpack` correctly resolved ACCEPTABLE |
| `scan_rpython_restrictions.py` | Unbounded `**kwargs`; `eval`/`exec` deliberately excluded | 145 findings |
| `scan_unvalidated_helper_calls.py` | User value into RPython helper without precondition check | Catches PYPY-FUZZ-002, REV-004, REV-005; clause-splitting checks range and surrogates independently |
| `scan_sibling_guard_consistency.py` | App-exposed method missing the guard convention its class establishes | Real findings on `W_BytesIO`, `W_StringIO`, `W_MemoryView` |
| `scan_cross_class_method_guard.py` | Same method name on sibling classes; some guard their field, one doesn't | Catches PYPY-REV-001 (`_dealloc_warn_w` segfault) |
| `scan_free_pairing.py` | Raw `free()` pairing and suspicious `ffi.*` attribute access in `lib_pypy/` | Catches `ffi.NONE` typo (PYPY-FUZZ-004) and unlocked free calls |
| `scan_guard_callback_deref.py` | Guard → app-level callback → guarded-field dereference (TOCTOU) | **4/4 recall, 0 false positives** on 4 confirmed SIGSEGVs |

**Agents** (`plugins/pypy-review-toolkit/agents/`):
`translated-divergence-auditor`, `immutability-contract-auditor`,
`interp-app-boundary-checker`, `rpython-restriction-scanner`,
`unvalidated-helper-call-auditor`, `jit-trace-reviewer`, `git-history-analyzer`.

**Commands** (`plugins/pypy-review-toolkit/commands/`):
`explore`, `health`, `hotspots`, `known-issues`.

**112 tests passing.**

## Requirements

```
pip install -r requirements.txt
```


**tree-sitter is required, not optional.** PyPy's RPython source is written in
Python 2 syntax on every branch — the py3.x branches implement Python 3, they
are not written in it — so a meaningful share of the tree fails `ast.parse()`
under a Python 3 host. Without `tree_sitter` + `tree_sitter_python`, those
files are skipped **silently**: scanners return a clean, confident, materially
incomplete result.

Measured on `py3.11` (`fe2af5843a`), share of `.py` files failing `ast.parse()`
under CPython 3.14:

| layer | files | ast.parse() failures |
|---|---|---|
| `pypy/interpreter` | 124 | **29.0%** |
| `rpython/rlib` | 253 | **26.1%** |
| `pypy/objspace` | 108 | **25.9%** |
| `rpython/memory` | 62 | 16.1% |
| `pypy/module` | 820 | 14.8% |
| `lib_pypy` | 189 | 10.6% |
| `rpython/jit` | 635 | 8.5% |

When tree-sitter is missing, every scanner's envelope carries a
`degraded_parse_mode` warning in `pypy_info` rather than leaving the gap
to be inferred from a `"tree_sitter_available": false` flag.

## Install

Not yet published to a marketplace. For local development:

```
claude --plugin-dir plugins/pypy-review-toolkit
```
