"""Fixture: @jit.unroll_safe function with range(len(lst)) — unbounded loop.

Expected finding: UNROLL_SAFE_UNBOUNDED (from scan_immutability_contracts.py)
"""
from rpython.rlib import jit


@jit.unroll_safe
def sum_items(lst):
    total = 0
    for i in range(len(lst)):
        total += lst[i]
    return total
