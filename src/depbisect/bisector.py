"""The two search algorithms: subset minimization and version bisection.

Both are pure: they know nothing about installs or subprocesses. The
caller hands them an oracle callable and they hand back a result plus
the number of oracle invocations, which makes them trivially testable
with mocked runners.

Subset minimization is delta debugging (Zeller's ddmin) over the set of
changed dependencies: the oracle answers "does the test pass when this
subset is reverted to its good versions?". Reverting everything is
known to pass and reverting nothing is known to fail, so ddmin shrinks
toward the smallest reverting set that still fixes the test.

Version bisection is classic binary search over an ordered candidate
list whose first element is known to pass and last is known to fail.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from depbisect.diffing import ChangedDep

SubsetOracle = Callable[[frozenset[str]], bool]
VersionOracle = Callable[[str], bool]


@dataclass
class SubsetResult:
    """Outcome of minimizing the reverting set."""

    culprits: list[ChangedDep]
    runs: int
    interaction: bool  # True when more than one dependency is required
    trace: list[tuple[frozenset[str], bool]] = field(default_factory=list)


def minimize_breaking_set(changed: Sequence[ChangedDep], oracle: SubsetOracle) -> SubsetResult:
    """Find the smallest set of changed deps whose revert fixes the test.

    ``oracle(names)`` must return True when the test passes with exactly
    the deps in ``names`` reverted to their good versions (all other
    changed deps left at their bad versions). Precondition, verified by
    the caller's baseline runs: oracle(all) is True, oracle(empty) is
    False.
    """
    by_name = {dep.name: dep for dep in changed}
    runs = 0
    trace: list[tuple[frozenset[str], bool]] = []
    cache: dict[frozenset[str], bool] = {
        frozenset(by_name): True,
        frozenset(): False,
    }

    def test(subset: frozenset[str]) -> bool:
        nonlocal runs
        if subset not in cache:
            result = oracle(subset)
            cache[subset] = result
            runs += 1
            trace.append((subset, result))
        return cache[subset]

    current = sorted(by_name)  # deterministic chunking order
    if len(current) == 1:
        return SubsetResult([by_name[current[0]]], 0, False, trace)

    granularity = 2
    while len(current) >= 2:
        chunk_size = max(1, len(current) // granularity)
        chunks = [current[i : i + chunk_size] for i in range(0, len(current), chunk_size)]
        reduced = False

        for chunk in chunks:
            if len(chunk) < len(current) and test(frozenset(chunk)):
                current, granularity, reduced = chunk, 2, True
                break
        if not reduced and granularity > 2:
            for chunk in chunks:
                complement = [name for name in current if name not in chunk]
                if complement and test(frozenset(complement)):
                    current, granularity, reduced = (
                        complement,
                        max(granularity - 1, 2),
                        True,
                    )
                    break
        if not reduced:
            if granularity >= len(current):
                break
            granularity = min(len(current), granularity * 2)

    culprits = [by_name[name] for name in sorted(current)]
    return SubsetResult(culprits, runs, len(culprits) > 1, trace)


@dataclass
class BisectResult:
    """Outcome of a version bisection along an ordered candidate path."""

    last_passing: str
    first_failing: str
    runs: int
    trace: list[tuple[str, bool]] = field(default_factory=list)


def bisect_versions(path: Sequence[str], oracle: VersionOracle) -> BisectResult:
    """Binary-search the pass/fail boundary along ``path``.

    ``path[0]`` is known to pass and ``path[-1]`` known to fail (from the
    baseline runs), so only interior candidates cost oracle calls.
    Assumes a single boundary; with flaky tests or multiple boundaries
    the answer is one valid boundary, not necessarily the only one.
    """
    if len(path) < 2:
        raise ValueError("need at least the good and bad versions")
    runs = 0
    trace: list[tuple[str, bool]] = []
    lo, hi = 0, len(path) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        passed = oracle(path[mid])
        runs += 1
        trace.append((path[mid], passed))
        if passed:
            lo = mid
        else:
            hi = mid
    return BisectResult(path[lo], path[hi], runs, trace)


def estimate_runs(n_changed: int, n_candidates: int) -> tuple[int, int]:
    """(best, worst) case run-count estimate for a full session.

    Two baseline runs always happen. The subset stage is skipped for a
    single changed dep; otherwise ddmin costs between one and roughly
    n^2 + 3n oracle calls (standard ddmin bound). The version stage
    costs ceil(log2) of the candidate gap.
    """
    baseline = 2
    if n_changed <= 1:
        subset_lo = subset_hi = 0
    else:
        subset_lo = 1
        subset_hi = n_changed * n_changed + 3 * n_changed
    interior = max(n_candidates - 2, 0)
    bisect_lo = 0 if interior == 0 else 1
    bisect_hi = 0
    while (1 << bisect_hi) < interior + 1:
        bisect_hi += 1
    return baseline + subset_lo + bisect_lo, baseline + subset_hi + bisect_hi
