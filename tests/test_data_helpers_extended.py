"""Schema and coverage tests for the expanded sensitive_rpython_helpers.json.

Validates that:
- The file parses and meets the minimum 30-entry target.
- Every multi-clause entry has consistent guard_pattern_hints (union of clause text_hints).
- Every confirmed_bug reference resolves to a row in pypy_known_bugs.tsv.
- Every qualname follows the module.function or module.Class.method pattern.
- Three newly-added helpers (rutf8.utf8chr, rbigint.frombytes, rfloat.string_to_float)
  are detected when called without guards in a synthetic file.
"""

from __future__ import annotations

import ast
import json
import re
import sys
import textwrap
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_DATA = _REPO / "plugins" / "pypy-review-toolkit" / "data"
_HELPERS_PATH = _DATA / "sensitive_rpython_helpers.json"
_KNOWN_BUGS_PATH = _DATA / "pypy_known_bugs.tsv"

sys.path.insert(0, str(_REPO / "plugins" / "pypy-review-toolkit" / "scripts"))


def _load() -> list[dict]:
    with _HELPERS_PATH.open(encoding="utf-8") as f:
        return json.load(f)["helpers"]


def _bug_ids() -> set[str]:
    ids: set[str] = set()
    with _KNOWN_BUGS_PATH.open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i == 0:
                continue
            parts = line.strip().split("\t")
            if parts and parts[0]:
                ids.add(parts[0].strip())
    return ids


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

def test_helpers_file_parses():
    helpers = _load()
    assert isinstance(helpers, list)
    assert len(helpers) > 0


def test_minimum_entry_count():
    helpers = _load()
    assert len(helpers) >= 30, f"Expected ≥30 entries; got {len(helpers)}"


def test_every_entry_has_required_fields():
    required = {"qualname", "source", "precondition", "confirmed_bug",
                "guard_pattern_hints", "exception_guard_hints"}
    for h in _load():
        missing = required - set(h.keys())
        assert not missing, f"Entry {h.get('qualname')} missing: {missing}"


def test_qualname_has_module_prefix():
    """qualname must be module.function or module.Class.method — no bare names."""
    pat = re.compile(r"^\w+\.\w+(\.\w+)?$")
    for h in _load():
        qn = h["qualname"]
        assert pat.match(qn), f"qualname {qn!r} does not match module.name pattern"


def test_confirmed_bug_ids_exist_in_tsv():
    """confirmed_bug must be null or a valid ID in pypy_known_bugs.tsv."""
    known = _bug_ids()
    for h in _load():
        cb = h["confirmed_bug"]
        if cb is None:
            continue
        # confirmed_bug may be a prose string containing one or more IDs
        found_ids = re.findall(r"PYPY-(?:FUZZ|REV)-\d+", cb)
        for bid in found_ids:
            assert bid in known, f"{h['qualname']}: confirmed_bug references {bid!r} not in tsv"


def test_clauses_have_name_field():
    """Every clause in a multi-clause entry must have a 'name' field."""
    for h in _load():
        for clause in h.get("clauses", []):
            assert "name" in clause, f"{h['qualname']}: clause missing 'name': {clause}"


def test_guard_pattern_hints_covers_clause_text_hints():
    """Top-level guard_pattern_hints must include all text from every clause's text_hints."""
    for h in _load():
        clauses = h.get("clauses")
        if not clauses:
            continue
        top = set(h.get("guard_pattern_hints", []))
        for clause in clauses:
            for hint in clause.get("text_hints", []):
                assert hint in top, (
                    f"{h['qualname']}: clause text_hint {hint!r} not in top-level "
                    f"guard_pattern_hints — add it for backward compatibility"
                )


def test_no_duplicate_qualnames():
    helpers = _load()
    seen: set[str] = set()
    for h in helpers:
        qn = h["qualname"]
        assert qn not in seen, f"Duplicate qualname: {qn!r}"
        seen.add(qn)


def test_source_paths_start_with_rpython_rlib():
    """Every source field should reference an rlib path (the scanner's scope)."""
    for h in _load():
        src = h["source"]
        assert src.startswith("rpython/rlib"), (
            f"{h['qualname']}: source {src!r} is outside rpython/rlib"
        )


# ---------------------------------------------------------------------------
# Scanner detection test: new helpers must actually be detected
# ---------------------------------------------------------------------------

def test_scanner_detects_new_helpers(tmp_path: Path):
    """The scanner must flag unguarded calls to three newly-added helpers."""
    # Build a synthetic lib_pypy-shaped directory so discover() finds files
    lib_pypy = tmp_path / "lib_pypy"
    lib_pypy.mkdir()

    src = textwrap.dedent("""\
        # synthetic file with three unguarded helper calls
        from rpython.rlib import rutf8, rfloat
        from rpython.rlib.rbigint import rbigint

        def unguarded_utf8chr(space, w_code):
            codepoint = space.int_w(w_code)
            return rutf8.utf8chr(codepoint)

        def unguarded_frombytes(space, w_data, w_byteorder):
            data = space.bytes_w(w_data)
            byteorder = space.text_w(w_byteorder)
            return rbigint.frombytes(data, byteorder, False)

        def unguarded_string_to_float(space, w_s):
            s = space.text_w(w_s)
            return rfloat.string_to_float(s)
    """)
    target = tmp_path / "pypy" / "module" / "test_helpers.py"
    target.parent.mkdir(parents=True)
    target.write_text(src, encoding="utf-8")

    # Also create the rpython/rlib tree so discover() recognises the checkout
    (tmp_path / "rpython" / "jit").mkdir(parents=True)
    (tmp_path / "pypy" / "interpreter").mkdir(parents=True)

    from scan_unvalidated_helper_calls import analyze

    result = analyze(str(tmp_path))
    findings = result.get("findings", [])
    flagged_qualnames = {f["message"].split()[2] for f in findings if "call to" in f["message"]}

    assert "rutf8.utf8chr" in flagged_qualnames, (
        f"Expected rutf8.utf8chr in findings; got {flagged_qualnames}"
    )
    assert "rbigint.frombytes" in flagged_qualnames, (
        f"Expected rbigint.frombytes in findings; got {flagged_qualnames}"
    )
    assert "rfloat.string_to_float" in flagged_qualnames, (
        f"Expected rfloat.string_to_float in findings; got {flagged_qualnames}"
    )
