"""Fixture: JitDriver with a jit_merge_point call present in the file.

Expected finding: zero findings.
"""
from rpython.rlib.jit import JitDriver

jitdriver = JitDriver(greens=[], reds=["x"])


def run(x):
    while x > 0:
        jitdriver.jit_merge_point(x=x)
        x -= 1
    return x
