"""Fixture: W_* class acquires a resource but has a descr_close method.

Expected finding: zero W_CLASS_NO_RELEASE findings.
"""


class W_Bar(object):
    def __init__(self):
        self.handle = open("somefile", "rb")

    def descr_close(self, space):
        self.handle.close()
