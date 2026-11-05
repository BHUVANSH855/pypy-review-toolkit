"""Fixture: W_ class with raw lltype.malloc() assigned to self.*, no trace hook.

Expected finding: RAW_ALLOC_NO_TRACE_HOOK
"""
from rpython.rtyper.lltypesystem import lltype

BUF = lltype.Struct("BUF", ("data", lltype.Signed))


class W_RawBuffer(object):
    def __init__(self, size):
        # Raw allocation stored as an instance attribute — GC cannot trace
        # pointers embedded in this memory.  No register_custom_trace_hook.
        self.buf = lltype.malloc(BUF, flavor="raw")
        self.size = size

    def release(self):
        if self.buf:
            lltype.free(self.buf, flavor="raw")
            self.buf = lltype.nullptr(BUF)
