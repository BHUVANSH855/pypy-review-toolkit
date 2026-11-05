"""Fixture: W_ class with raw allocation AND rgc.register_custom_trace_hook.

Expected finding: zero findings.
"""
from rpython.rlib import rgc
from rpython.rtyper.lltypesystem import lltype

BUF = lltype.Struct("BUF", ("data", lltype.Signed))


class W_TracedBuffer(object):
    def __init__(self, size):
        self.buf = lltype.malloc(BUF, flavor="raw")
        self.size = size

    @staticmethod
    def _trace_func(gc, buf_ptr, callback, arg):
        pass  # trace embedded GC refs (none here, illustrative)

    # Trace hook registered — the GC knows about this raw allocation
    rgc.register_custom_trace_hook(W_TracedBuffer, W_TracedBuffer._trace_func)

    def release(self):
        if self.buf:
            lltype.free(self.buf, flavor="raw")
            self.buf = lltype.nullptr(BUF)
