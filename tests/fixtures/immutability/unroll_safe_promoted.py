"""Fixture: @jit.unroll_safe with jit.promote() — bounded loop, no finding.

Expected finding: zero UNROLL_SAFE_UNBOUNDED findings.
"""
from rpython.rlib import jit


@jit.unroll_safe
def sum_items_safe(lst):
    n = jit.promote(len(lst))
    total = 0
    for i in range(n):
        total += lst[i]
    return total
