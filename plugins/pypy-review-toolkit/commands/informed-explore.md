---
name: informed-explore
description: "Reachability-weighted exploration — same as /explore but prioritises HIGH-reachability findings"
argument-hint: "[scope] [aspect] [depth]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task"]
---

# PyPy Review Toolkit — Informed Explore

Runs the full `/explore` pipeline but weights triage by reachability: findings
in functions known to be reachable from user Python code are promoted to the
front of each agent's review queue and annotated `"reachability": "HIGH"`.
Findings with no matching entry in the reachability map are labelled
`"reachability": "UNKNOWN"` — not safe to dismiss, but reviewed after `HIGH`
findings.

**Scope:** "$ARGUMENTS" (default: same in-scope surface as `/explore`).

## Reachability levels

| Level | Meaning |
|-------|---------|
| `HIGH` | Enclosing function or class matches a `qualname` in `rpython_reachability_sources.json` with `"confirmed": true` |
| `UNKNOWN` | No confirmed reachability entry found — may still be reachable via an indirect call chain not yet catalogued |

`UNKNOWN` does **not** mean unreachable. The reachability data file is
intentionally incomplete and manually maintained. A finding at an `UNKNOWN`
site is still worth reviewing; it simply has lower confirmed priority than
`HIGH`.

## Steps

### Step 1 — Discovery (same as /explore Phase 1)

Run `discover_pypy.py` on the target. Confirm this is a recognised PyPy
checkout. Resolve the commit hash for the scan header.

### Step 2 — Reachability-weighted scanner pass

Run `scan_unvalidated_helper_calls.py` with `--reachability-map` pointing to
`data/rpython_reachability_sources.json`:

```bash
python plugins/pypy-review-toolkit/scripts/scan_unvalidated_helper_calls.py \
    "$ARGUMENTS" \
    --reachability-map plugins/pypy-review-toolkit/data/rpython_reachability_sources.json
```

Each finding in the output will carry `"reachability": "HIGH"` or
`"reachability": "UNKNOWN"`. Sort findings by reachability (`HIGH` first) before
passing them to the agent.

Run all other scanners without the reachability flag (they do not yet support
it):

```bash
python plugins/pypy-review-toolkit/scripts/scan_translated_divergence.py "$ARGUMENTS"
python plugins/pypy-review-toolkit/scripts/scan_immutability_contracts.py "$ARGUMENTS"
python plugins/pypy-review-toolkit/scripts/scan_interp_app_boundary.py "$ARGUMENTS"
python plugins/pypy-review-toolkit/scripts/scan_rpython_restrictions.py "$ARGUMENTS"
```

### Step 3 — Prioritised agent triage

For each scanner's findings, process `HIGH`-reachability findings first:

1. Read the enclosing function in the PyPy source.
2. Confirm whether the finding is a real bug (FIX), a likely improvement
   (CONSIDER), a design decision (POLICY), or intentional (ACCEPTABLE).
3. For `HIGH` findings: explicitly state whether a reachable exploit path
   exists (i.e., can a user Python value reach this code path without special
   permissions?).
4. Then process `UNKNOWN` findings with the same triage criteria.

### Step 4 — Differential (optional, if baseline available)

If a previous scan JSON is available, run `run_differential.py` to identify
regressions (new findings since the baseline) and improvements (resolved
findings):

```bash
python plugins/pypy-review-toolkit/scripts/run_differential.py \
    baseline.json current.json
```

New findings should be triaged immediately. Resolved findings confirm that a
fix or suppression was effective.

### Step 5 — Synthesis

Produce a prioritised finding report:

```
Informed Explore — <checkout_root> (<commit>)

HIGH-reachability findings:  <N>
  FIX:        <N>
  CONSIDER:   <N>
  POLICY:     <N>
  ACCEPTABLE: <N>

UNKNOWN-reachability findings: <N>
  (counts by classification)

Differential vs baseline: <N> new, <N> resolved (if baseline provided)
```

List all HIGH FIX findings first, with file, line, and a one-sentence
description of the exploit path.
