#!/usr/bin/env python3
"""Diff two scanner JSON envelope outputs to find new, resolved, and unchanged findings.

A finding is identified by the tuple (file, type, line). A finding is:
  - "new"      if it appears in current but not baseline
  - "resolved" if it appears in baseline but not current
  - "unchanged" if it appears in both (same file, type, and line)

Output JSON schema:
    {
        "new":             [ <finding>, ... ],
        "resolved":        [ <finding>, ... ],
        "unchanged_count": <int>
    }

Usage:
    python run_differential.py baseline.json current.json [--output out.json]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _finding_key(f: dict) -> tuple[str, str, int]:
    """Return the identity tuple for a finding."""
    return (str(f.get("file", "")), str(f.get("type", "")), int(f.get("line", 0)))


def differential(baseline_envelope: dict, current_envelope: dict) -> dict:
    """Compute the diff between two scanner envelope outputs.

    Parameters
    ----------
    baseline_envelope:
        JSON envelope from a previous scanner run.
    current_envelope:
        JSON envelope from the current scanner run.

    Returns
    -------
    dict with keys ``new``, ``resolved``, ``unchanged_count``.
    """
    baseline_findings: list[dict] = baseline_envelope.get("findings", [])
    current_findings: list[dict] = current_envelope.get("findings", [])

    baseline_by_key: dict[tuple, dict] = {_finding_key(f): f for f in baseline_findings}
    current_by_key: dict[tuple, dict] = {_finding_key(f): f for f in current_findings}

    baseline_keys = set(baseline_by_key)
    current_keys = set(current_by_key)

    new_keys = current_keys - baseline_keys
    resolved_keys = baseline_keys - current_keys
    unchanged_keys = baseline_keys & current_keys

    return {
        "new": [current_by_key[k] for k in sorted(new_keys)],
        "resolved": [baseline_by_key[k] for k in sorted(resolved_keys)],
        "unchanged_count": len(unchanged_keys),
    }


def main() -> None:
    argv = sys.argv[1:]
    if len(argv) < 2:
        sys.stderr.write(
            "Usage: run_differential.py baseline.json current.json [--output out.json]\n"
        )
        sys.exit(2)

    baseline_path = Path(argv[0])
    current_path = Path(argv[1])

    output_path: Path | None = None
    i = 2
    while i < len(argv):
        if argv[i] == "--output" and i + 1 < len(argv):
            output_path = Path(argv[i + 1])
            i += 2
        else:
            i += 1

    try:
        baseline_envelope = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"Error reading baseline {baseline_path}: {exc}\n")
        sys.exit(1)

    try:
        current_envelope = json.loads(current_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"Error reading current {current_path}: {exc}\n")
        sys.exit(1)

    result = differential(baseline_envelope, current_envelope)
    output = json.dumps(result, indent=2)

    if output_path:
        output_path.write_text(output, encoding="utf-8")
    else:
        print(output)


if __name__ == "__main__":
    main()
