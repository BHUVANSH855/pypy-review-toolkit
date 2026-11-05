"""Fixture: JitDriver declaration and call sites match exactly.

Expected finding: zero findings.
"""
from rpython.rlib.jit import JitDriver

jitdriver = JitDriver(greens=["bytecode"], reds=["pc", "frame"])


def interpret(bytecode, pc, frame):
    while True:
        jitdriver.jit_merge_point(bytecode=bytecode, pc=pc, frame=frame)
        op = bytecode[pc]
        pc += 1
        if op == 0:
            break
    return frame
