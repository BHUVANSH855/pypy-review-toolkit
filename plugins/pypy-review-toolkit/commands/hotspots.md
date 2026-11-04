---
description: "Where to look first — flagship + crown-jewel findings, ranked"
argument-hint: "[scope]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task"]
---

# PyPy Review Toolkit — Hotspots

Surface the highest-signal findings first: `translated-divergence-auditor` and
`immutability-contract-auditor` — the flagship and crown jewel, the two agents
with real, individually-traced evidence behind them (see the design doc's
investigation trail).

**Scope:** "$ARGUMENTS" (default: entire in-scope surface)

## Workflow

1. Run `scan_translated_divergence.py` and `scan_immutability_contracts.py`.
2. Dispatch `translated-divergence-auditor` and `immutability-contract-auditor`
   for real review of every FIX and CONSIDER/high finding.
3. After the scanner pass, cross-reference `data/rpython_known_patterns.json`:
   for each hotspot location, check whether any confirmed shape has a
   `confirmed_instance` whose bug entry in `pypy_known_bugs.tsv` points to the
   same file or module. If yes, include a **historical signal** line alongside
   the finding, e.g.:
   > _"This file is the confirmed location of PYPY-FUZZ-004 (Shape B —
   > cffi-free-pairing). The same shape can recur."_
   This turns the hotspot list from a raw density ranking into one that surfaces
   confirmed bug history alongside scanner findings.
4. Rank output:
   - `immutability-contract-mismatch-promoted` findings first (strongest
     evidence tier — a live object read by JIT-promoted code)
   - `we-are-translated-two-arm-divergence` findings classified FIX
   - Remaining CONSIDER findings from both agents, ordered by file churn if
     `analyze_history.py` data is available (a high-churn file with a
     CONSIDER finding is a stronger priority than a stable one)
5. Present as a ranked list, not a table — hotspots are about "look here
   first," not a dashboard.

## Usage

```
/pypy-review-toolkit:hotspots
/pypy-review-toolkit:hotspots pypy/module/_cppyy
```
