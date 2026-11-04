"""Schema-validation tests for data/rpython_known_patterns.json.

Verifies that the file exists, parses cleanly, and that every field
referenced by the hotspots command and future informed-explore command
is present and coherent.
"""

from __future__ import annotations

import json
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent / "plugins" / "pypy-review-toolkit" / "data"
_PATTERNS_PATH = _DATA_DIR / "rpython_known_patterns.json"
_KNOWN_BUGS_PATH = _DATA_DIR / "pypy_known_bugs.tsv"
_EXPECTED_SHAPE_IDS = {"SHAPE-A", "SHAPE-B", "SHAPE-C", "SHAPE-D", "SHAPE-E"}


def _load() -> tuple[dict, list[dict]]:
    with _PATTERNS_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    return data, data["shapes"]


def _load_known_bug_ids() -> set[str]:
    """Return all bug IDs from pypy_known_bugs.tsv (first column, skip header)."""
    ids: set[str] = set()
    with _KNOWN_BUGS_PATH.open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i == 0:
                continue  # header row
            parts = line.strip().split("\t")
            if parts and parts[0]:
                ids.add(parts[0].strip())
    return ids


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

def test_patterns_file_parses():
    """The file must exist and parse without error."""
    data, shapes = _load()
    assert isinstance(shapes, list)
    assert len(shapes) > 0


def test_all_five_shape_ids_present():
    """All five shape IDs must appear in the file."""
    _, shapes = _load()
    found = {s["id"] for s in shapes}
    assert _EXPECTED_SHAPE_IDS == found, f"Missing or extra shapes: {found ^ _EXPECTED_SHAPE_IDS}"


def test_every_shape_has_required_fields():
    """Every shape entry must carry all required fields."""
    required = {"id", "name", "description", "confirmed_instances", "scanner",
                "data_file", "stderr_oracle", "default_classification"}
    _, shapes = _load()
    for shape in shapes:
        missing = required - set(shape.keys())
        assert not missing, f"Shape {shape.get('id')} missing fields: {missing}"


def test_confirmed_instances_reference_known_bug_ids():
    """Every confirmed_instance ID must exist in pypy_known_bugs.tsv."""
    _, shapes = _load()
    known_ids = _load_known_bug_ids()
    for shape in shapes:
        for bug_id in shape["confirmed_instances"]:
            assert bug_id in known_ids, (
                f"Shape {shape['id']}: confirmed_instance {bug_id!r} not in pypy_known_bugs.tsv"
            )


def test_shape_a_has_stderr_oracle():
    """Shape A must have a non-null, non-empty stderr_oracle."""
    _, shapes = _load()
    shape_a = next(s for s in shapes if s["id"] == "SHAPE-A")
    assert shape_a["stderr_oracle"] is not None
    assert len(shape_a["stderr_oracle"]) > 0


def test_non_shape_a_stderr_oracle_is_null():
    """All shapes except Shape A must have stderr_oracle set to null."""
    _, shapes = _load()
    for shape in shapes:
        if shape["id"] != "SHAPE-A":
            assert shape["stderr_oracle"] is None, (
                f"Shape {shape['id']} unexpectedly has stderr_oracle: {shape['stderr_oracle']!r}"
            )


def test_data_file_non_null_exists():
    """Every shape whose data_file is non-null must have that file in data/."""
    _, shapes = _load()
    for shape in shapes:
        df = shape["data_file"]
        if df is not None:
            path = _DATA_DIR / df
            assert path.exists(), (
                f"Shape {shape['id']}: data_file {df!r} not found at {path}"
            )


def test_default_classification_valid():
    """default_classification must be FIX or CONSIDER."""
    _, shapes = _load()
    valid = {"FIX", "CONSIDER"}
    for shape in shapes:
        assert shape["default_classification"] in valid, (
            f"Shape {shape['id']}: invalid default_classification {shape['default_classification']!r}"
        )


def test_schema_version_present():
    """The top-level schema_version field must be present."""
    data, _ = _load()
    assert "schema_version" in data
    assert data["schema_version"] == "1.0"
