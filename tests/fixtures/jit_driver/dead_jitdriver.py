"""Fixture: JitDriver defined but merge-point wiring was never completed.

Expected finding: DEAD_JITDRIVER
"""
from rpython.rlib.jit import JitDriver

# Driver declared but the loop it was meant to accelerate was removed,
# or the wiring was never completed.
jitdriver = JitDriver(greens=[], reds=["x"])


def run(x):
    while x > 0:
        x -= 1
    return x
