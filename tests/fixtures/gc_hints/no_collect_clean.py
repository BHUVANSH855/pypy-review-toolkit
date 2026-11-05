"""Fixture: @rgc.no_collect function with no allocations.

Expected finding: zero findings.
"""
from rpython.rlib import rgc


@rgc.no_collect
def add_ints(a, b):
    # Pure arithmetic — no GC allocation, no W_ construction
    return a + b
