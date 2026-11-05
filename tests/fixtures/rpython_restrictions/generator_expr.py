"""Fixture: function containing a generator expression.

Expected finding: RPYTHON_GENERATOR_EXPR
"""


def total(lst):
    return sum(x * 2 for x in lst)
