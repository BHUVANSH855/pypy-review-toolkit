"""Fixture: function containing a mixed int/str list literal.

Expected finding: RPYTHON_MIXED_CONTAINER
"""


def build_table():
    return [1, "two", 3]
