"""Fixture: @rgc.no_collect function with allocation under we_are_translated() guard.

The allocation only runs on CPython (untranslated); in the translated binary
the if-branch is never taken.  Expected: zero findings or NO_COLLECT_SUSPECT
(either is acceptable — documented in the test).
"""
from rpython.rlib import rgc
from rpython.rlib.objectmodel import we_are_translated


@rgc.no_collect
def debug_only_alloc():
    if not we_are_translated():
        # This branch is dead in the translated binary; the allocation
        # never runs where @no_collect matters.
        result = [1, 2, 3]
        return result
    return None
