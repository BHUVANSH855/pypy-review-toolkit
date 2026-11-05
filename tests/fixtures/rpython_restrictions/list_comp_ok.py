"""Fixture: function containing a list comprehension (OK in RPython).

Expected finding: zero RPYTHON_MIXED_CONTAINER or RPYTHON_GENERATOR_EXPR findings.
"""


def doubled(lst):
    return [x * 2 for x in lst]
