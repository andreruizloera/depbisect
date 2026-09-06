"""Evaluate a ``Requires-Python`` specifier against an interpreter version.

An index reports, per distribution file, which interpreters it declares
support for. depbisect uses that to drop candidate releases it could
never install on the interpreter the trials will actually run, because
an install failure mid-bisect looks exactly like a test failure unless
something rules it out first.

This is a deliberately small PEP 440 subset: the operators that appear
in real ``Requires-Python`` values (``>= > <= < == != ~= ===``), comma
separated, with prefix matching (``==3.9.*``). depbisect carries it
instead of depending on ``packaging`` for the same reason it carries its
own comparator: zero runtime dependencies.

The rule for anything this cannot read is UNKNOWN IS NOT NO. A clause
that does not parse contributes nothing and the readable clauses still
decide, so the release stays in the candidate list unless something we
actually understood ruled it out. Erring the other way would silently
delete real releases from the search on a specifier shape we had not
seen. Note that an unreadable clause is SKIPPED rather than treated as
an immediate pass, which is what keeps the answer independent of the
order the clauses happen to be written in.
"""

from __future__ import annotations

import re

from depbisect.versions import VersionParseError, parse_version

_CLAUSE_RE = re.compile(r"^\s*(===|~=|==|!=|<=|>=|<|>)\s*([^\s,]+)\s*$")


def python_supported(spec: str | None, python: tuple[int, ...]) -> bool:
    """Does an interpreter of version ``python`` satisfy ``spec``?

    ``spec`` is a Requires-Python string such as ``">=3.8"`` or
    ``">=3.7,<4"``. None, empty, or unparseable means "no stated
    constraint", which is True.
    """
    if not spec or not spec.strip():
        return True
    text = ".".join(str(part) for part in python)
    for clause in spec.split(","):
        if not clause.strip():
            continue
        m = _CLAUSE_RE.match(clause)
        if not m:
            continue  # unknown is not no: an unreadable clause says nothing
        try:
            if not _clause_holds(m.group(1), m.group(2), text):
                return False
        except VersionParseError:
            continue
    return True


def _clause_holds(op: str, bound: str, actual: str) -> bool:
    if op == "===":
        return actual == bound.strip()
    if bound.endswith(".*"):
        return _prefix_clause(op, bound[:-2], actual)
    if op == "~=":
        # ~=X.Y means >=X.Y and ==X.*; ~=X.Y.Z means >=X.Y.Z and ==X.Y.*
        release = parse_version(bound).release
        stated = len(bound.split("+", 1)[0].split("."))
        if stated < 2:
            raise VersionParseError(f"~= needs at least two release segments: {bound!r}")
        prefix = ".".join(str(part) for part in release[: stated - 1])
        return _compare(actual, bound) >= 0 and _prefix_clause("==", prefix, actual)

    result = _compare(actual, bound)
    return {
        "==": result == 0,
        "!=": result != 0,
        "<=": result <= 0,
        ">=": result >= 0,
        "<": result < 0,
        ">": result > 0,
    }[op]


def _prefix_clause(op: str, prefix: str, actual: str) -> bool:
    """Handle ``==X.Y.*`` and ``!=X.Y.*``: compare only the stated segments."""
    if op not in ("==", "!="):
        raise VersionParseError(f"operator {op!r} does not take a wildcard version")
    width = len(prefix.split("."))
    a = parse_version(actual).release[:width]
    b = parse_version(prefix).release[:width]
    return (a == b) if op == "==" else (a != b)


def _compare(a: str, b: str) -> int:
    va, vb = parse_version(a), parse_version(b)
    if va < vb:
        return -1
    if vb < va:
        return 1
    return 0
