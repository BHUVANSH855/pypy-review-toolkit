"""Fixture: W_ class with ffi.new() assigned to self.ptr, no trace hook.

Expected finding: RAW_ALLOC_NO_TRACE_HOOK
"""
import cffi

ffi = cffi.FFI()
ffi.cdef("typedef struct { int x; } Point;")


class W_FfiPoint(object):
    def __init__(self, x):
        # ffi.new() stored as instance attribute — GC-invisible allocation
        # without a matching register_custom_trace_hook.
        self.ptr = ffi.new("Point *")
        self.ptr.x = x

    def get_x(self):
        return self.ptr.x
