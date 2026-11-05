"""Fixture: JitDriver with greens=['x'], reds=['y']; merge point passes wrong args.

Expected finding: JITDRIVER_ARG_MISMATCH
"""
from rpython.rlib.jit import JitDriver

jitdriver = JitDriver(greens=["x"], reds=["y"])


def interpret(x, y, z):
    while True:
        # Passes x and z — z is not declared; y is missing
        jitdriver.jit_merge_point(x=x, z=z)
        y -= 1
        if y <= 0:
            break
    return x
