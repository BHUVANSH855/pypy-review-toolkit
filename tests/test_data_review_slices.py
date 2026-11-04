"""Validate the committed data/review_slices.json manifest.

This test exists to catch schema drift: if discover_pypy.py adds or removes
a field, the committed manifest should be regenerated and re-committed.  The
test fails (loudly) rather than silently accepting a stale or malformed file.

Required fields per slice (design doc §13 / issue #13):
  layer, files, approx_lines, slice_id, review_priority

Layer coverage: every layer in IN_SCOPE_LAYERS must appear at least once.
"""

import json
import sys
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent / "plugins" / "pypy-review-toolkit" / "scripts"),
)

from discover_pypy import IN_SCOPE_LAYERS  # noqa: E402

_MANIFEST = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "pypy-review-toolkit"
    / "data"
    / "review_slices.json"
)

_REQUIRED_SLICE_FIELDS = {"layer", "files", "approx_lines", "slice_id", "review_priority"}
_VALID_PRIORITIES = {"critical", "high", "normal"}


def _load_manifest() -> tuple[dict, list[dict]]:
    """Load the manifest; return (full_data, slices_list).

    The file is the full discovery envelope: slices live under
    ``data["review_slices"]["slices"]``.
    """
    assert _MANIFEST.exists(), (
        f"review_slices.json not found at {_MANIFEST}. "
        "Run: python plugins/pypy-review-toolkit/scripts/discover_pypy.py /path/to/pypy --slices"
    )
    with _MANIFEST.open(encoding="utf-8") as fh:
        data = json.load(fh)
    slices = data.get("review_slices", {}).get("slices", [])
    return data, slices


def test_manifest_has_slices():
    """The manifest must contain at least one slice."""
    _, slices = _load_manifest()
    assert len(slices) > 0, "review_slices.json has no slices under review_slices.slices"


def test_manifest_slice_count_in_expected_range():
    """slice_count should be in the 60–120 range for a real PyPy checkout."""
    data, _ = _load_manifest()
    count = data.get("review_slices", {}).get("slice_count", 0)
    assert 60 <= count <= 120, (
        f"slice_count={count} outside expected range 60–120. "
        "Was the manifest generated against a real PyPy checkout?"
    )


def test_manifest_all_slices_have_required_fields():
    """Every slice must carry layer, files, approx_lines, slice_id, review_priority."""
    _, slices = _load_manifest()
    for i, s in enumerate(slices):
        missing = _REQUIRED_SLICE_FIELDS - set(s.keys())
        assert not missing, f"Slice {i} ({s.get('slice_id', '?')}) missing fields: {missing}"


def test_manifest_all_priorities_are_valid():
    """review_priority must be one of critical / high / normal."""
    _, slices = _load_manifest()
    for s in slices:
        prio = s.get("review_priority")
        assert prio in _VALID_PRIORITIES, (
            f"Slice {s.get('slice_id', '?')} has invalid review_priority={prio!r}"
        )


def test_manifest_io_slices_are_critical():
    """Any slice containing pypy/module/_io files must have review_priority=critical."""
    _, slices = _load_manifest()
    for s in slices:
        if any(f.startswith("pypy/module/_io") for f in s.get("files", [])):
            assert s.get("review_priority") == "critical", (
                f"Slice {s.get('slice_id', '?')} has _io files but priority={s.get('review_priority')!r}"
            )


def test_manifest_all_in_scope_layers_present():
    """Every layer in IN_SCOPE_LAYERS must appear at least once in the manifest."""
    _, slices = _load_manifest()
    present = {s["layer"] for s in slices}
    missing = IN_SCOPE_LAYERS - present
    assert not missing, f"Layers missing from manifest: {missing}"


def test_manifest_slice_ids_are_unique():
    """slice_id values must be unique across the manifest."""
    _, slices = _load_manifest()
    ids = [s.get("slice_id") for s in slices]
    assert len(ids) == len(set(ids)), "Duplicate slice_id values found in manifest"


def test_manifest_files_lists_are_nonempty():
    """Every slice must contain at least one file."""
    _, slices = _load_manifest()
    for s in slices:
        assert s.get("files"), f"Slice {s.get('slice_id', '?')} has an empty files list"


def test_manifest_has_git_commit_date():
    """The manifest envelope must include git_commit_date."""
    data, _ = _load_manifest()
    assert "git_commit_date" in data, "review_slices.json missing git_commit_date at top level"
    assert data["git_commit_date"] is not None, "git_commit_date is None — was git available?"
