"""Fixture: W_* class acquires a resource with no release method.

Expected finding: W_CLASS_NO_RELEASE
"""


class W_Foo(object):
    def __init__(self):
        self.handle = open("somefile", "rb")
        self.x = 42
