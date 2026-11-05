"""Fixture: @jit.unroll_safe with jit.promote() applied before the loop.

The length is promoted to a compile-time constant, so the JIT can safely
unroll.  Expected finding: zero findings.
"""
from rpython.rlib import jit


@jit.unroll_safe
def sum_items_safe(lst):
    # Promote the length — now the JIT knows the exact iteration count at
    # trace time and can unroll safely.
    n = jit.promote(len(lst))
    total = 0
    for i in range(n):
        total += lst[i]
    return total
