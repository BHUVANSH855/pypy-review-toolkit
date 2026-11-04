---
description: "Full exploration — discovery, history context, all agents, triage, synthesis"
argument-hint: "[scope] [aspect] [depth]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task"]
---

# PyPy Review Toolkit — Explore

Full review pipeline, phased so later agents can use earlier agents' output as
context — same shape as `cpython-review-toolkit`'s phase table.

**Scope:** "$ARGUMENTS" (default: the recognized in-scope surface — rlib, jit,
gc, interpreter, objspace, module, and lib_pypy; annotator/rtyper/translator
explicitly excluded unless separately supported).

## Phases

### Phase 1 — Discovery

Run `discover_pypy.py --slices`. Confirm this is a recognized PyPy checkout
before proceeding. If the in-scope surface is large enough to need review-slice
partitioning (real numbers: ~687K–867K lines in scope, roughly 2–2.4x
`cpython-review-toolkit`'s ~358K-line surface, which needed 37 slices at that
smaller size), work through `review_slices.json` slice by slice rather than
attempting the whole tree in one pass.

Confirm the discovered layers include the requested scope. Do not assume that
a scanner supports a layer merely because discovery recognizes it.

Before running checkout-dependent tools, resolve the recognized PyPy
checkout to an absolute path and set `PYPY_ROOT` to that path. Verify
that it is the intended checkout and record its branch and commit.
Use the same value consistently throughout the review.

### Phase 2 — History Context

Run `analyze_history.py` against the verified PyPy checkout identified in Phase 1. Pass the absolute checkout path as the positional argument and capture the complete JSON output, including its `notes`, warnings, and truncation information.

```bash
python plugins/pypy-review-toolkit/scripts/analyze_history.py "$PYPY_ROOT"
```

Here, `PYPY_ROOT` must point to the selected PyPy checkout, not the toolkit repository. Check the command's exit status and inspect the JSON for an `error` field before using the results. Preserve the complete output for `git-history-analyzer` in Phase 7.

Provide relevant temporal context from the history results to the other agents, but do not treat co-change patterns or commit history alone as proof of a defect. Use the default 90-day window unless the review explicitly requires a different supported time range. Only pass documented options to `analyze_history.py`; unknown flags are ignored and can produce misleading analysis.

### Phase 3 — Flagship + Crown Jewel

Run `scan_translated_divergence.py` and `scan_immutability_contracts.py`.
Dispatch `translated-divergence-auditor` and `immutability-contract-auditor`.
These are the two agents with real, individually traced evidence behind every
finding class — prioritize their output.

### Phase 4 — Boundary + Restriction

Run `scan_interp_app_boundary.py` and `scan_rpython_restrictions.py`. Dispatch
`interp-app-boundary-checker` and `rpython-restriction-scanner`. Remember the
restriction scanner's narrow scope (kwargs only) when reporting — do not imply
broader coverage than exists.

### Phase 5 — Qualitative

Dispatch `jit-trace-reviewer` (no backing script — judgment only, and should be
reported with appropriately lower confidence than scanner-backed findings).

### Phase 6 — CFFI and Guard-Consistency Triage

Run each scanner from the toolkit repository against the recognized PyPy
checkout. Pass the checkout path explicitly as the scanner's positional
argument. Capture each scanner's complete JSON output and check its exit
status before dispatching an auditor.

```bash
python plugins/pypy-review-toolkit/scripts/scan_free_pairing.py "$PYPY_ROOT"
python plugins/pypy-review-toolkit/scripts/scan_cross_class_method_guard.py "$PYPY_ROOT"
python plugins/pypy-review-toolkit/scripts/scan_sibling_guard_consistency.py "$PYPY_ROOT"
```

Here, `PYPY_ROOT` is the verified absolute path to the PyPy checkout selected
during Phase 1. Do not run these commands without the target path: otherwise,
the scanners may analyze the toolkit repository instead.

Invoke each named auditor as a separate agent task using the available
subagent-dispatch mechanism. Do not merely describe or recommend dispatching
the agent. Pass each auditor the matching scanner's complete JSON output, the
verified target checkout path, and the checkout's branch and commit when
available. Wait for each auditor to finish and capture its complete result
before proceeding to Phase 7. If agent dispatch is unavailable or fails,
report that limitation explicitly; do not claim the auditor completed.

- `scan_free_pairing.py` → `lib-pypy-cffi-auditor`
- `scan_cross_class_method_guard.py` → `cross-class-guard-auditor`
- `scan_sibling_guard_consistency.py` → `sibling-guard-auditor`

For each auditor, provide the scanner's original finding type, classification,
confidence, file, line, message, and detail. The auditor must inspect the
actual source at that checkout and trace relevant callers, control flow,
resource lifecycle, or guard invariants as appropriate. It must return a
classification, source evidence, reachability assessment, false-positive
analysis, confidence, rationale, and a recommended fix or next verification
step.

Do not send one scanner's findings to a different auditor unless there is a
specific, documented reason. If a scanner produces no findings, report that
result; do not invent findings. If a scanner fails, preserve its error output
and report the scanner as unavailable rather than treating the failure as a
clean scan.

Treat scanner output as leads, not confirmed bugs. Preserve the original
scanner message, file, and line in the triage result. Do not promote a finding
to FIX solely because a structural pattern was detected.

### Phase 7 — History-Last Synthesis

After every other review agent has completed, invoke `git-history-analyzer`
as a separate agent task using the available subagent-dispatch mechanism.
Wait for its result before Phase 8. Do not merely describe or recommend
dispatching it. Provide it with:

- The complete JSON output from `analyze_history.py`, including `notes` and
  any commit-cap or time-window limitations.
- The verified PyPy checkout path and its branch and commit, when available.
- The classified findings from all other agents, including the three Phase 6
  auditors.
- Each finding's original file, line, classification, confidence, and
  supporting source evidence.

Ask the history agent to correlate findings with relevant commits, fixes,
regressions, and co-change patterns where evidence exists. It must distinguish
confirmed historical evidence from hypotheses and report insufficient history
when appropriate. History can strengthen or weaken a finding, but must not
independently promote a scanner lead to a confirmed defect.

If history analysis failed or returned an error, report that limitation
explicitly rather than presenting the history review as completed.

For guard-consistency findings, use the auditors' classified results as the
starting point for temporal investigation. Use history to assess fix
completeness, similar-bug patterns, relevant changes, and churn × risk
prioritization. Raw scanner output alone is not a confirmed bug report.

### Phase 8 — Synthesis

Combine the results into a single report: findings grouped by classification
(FIX first), then by agent. Retain source evidence, confidence, and known
limitations. Do not imply that unreviewed layers or scanner blind spots have
been covered.

Note the toolkit's current honesty gaps explicitly rather than letting silence
imply completeness:

- `translated-divergence-auditor` is the flagship as a **working hypothesis**,
  not confirmed — this may pivot.
- No PyPy-specific known-bugs catalog exists yet (see `/known-issues`).
- The restriction scanner covers unbounded `**kwargs` only.
- The three Phase 6 scanners identify candidate patterns; their output does
  not independently establish a defect.

## Usage

```text
/pypy-review-toolkit:explore
/pypy-review-toolkit:explore pypy/module/_cppyy
/pypy-review-toolkit:explore rpython/rlib deep
/pypy-review-toolkit:explore lib_pypy
```
