"""Errors raised for expected failure modes.

Anything that is a user-facing problem (bad flags, missing files, git
trouble) raises DepbisectError. The CLI catches it, prints a clean
message, and exits nonzero. Real bugs still traceback.
"""


class DepbisectError(Exception):
    """An expected, user-facing failure. Printed without a traceback."""
