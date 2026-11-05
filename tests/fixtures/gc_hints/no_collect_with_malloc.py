"""Fixture: @rgc.no_collect function with a GC-managed lltype.malloc() call.

Expected finding: NO_COLLECT_VIOLATION
"""
from rpython.rlib import rgc
from rpython.rtyper.lltypesystem import lltype

TP = lltype.GcStruct("TP", ("x", lltype.Signed))


@rgc.no_collect
def build_tp():
    # lltype.malloc() without flavor='raw' — GC-managed allocation
    p = lltype.malloc(TP)
    p.x = 42
    return p
