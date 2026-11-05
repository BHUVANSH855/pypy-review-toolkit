"""Fixture: @jit.unroll_safe function with range(len(lst)) where lst is a parameter.

Expected finding: UNROLL_SAFE_UNBOUNDED
"""
from rpython.rlib import jit


@jit.unroll_safe
def sum_items(lst):
    # lst is a plain parameter — bound is unknown at trace time, causing
    # potential trace explosion.
    total = 0
    for i in range(len(lst)):
        total += lst[i]
    return total
