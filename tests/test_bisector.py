"""The search algorithms, exercised with mocked oracles only.

No installs, no subprocesses, no network: the oracle is a plain
function that simulates which dependency states pass.
"""

import pytest

from depbisect.bisector import (
    bisect_versions,
    estimate_runs,
    minimize_breaking_set,
)
from depbisect.diffing import ChangedDep


def deps(*names: str) -> list[ChangedDep]:
    return [ChangedDep(n, "1.0", "2.0") for n in names]


class CountingOracle:
    """Test passes iff every dep in ``breaking`` is reverted."""

    def __init__(self, breaking: set[str]) -> None:
        self.breaking = breaking
        self.calls: list[frozenset[str]] = []

    def __call__(self, reverted: frozenset[str]) -> bool:
        self.calls.append(reverted)
        return self.breaking <= reverted


class TestSubsetMinimization:
    def test_single_changed_dep_needs_no_runs(self) -> None:
        oracle = CountingOracle({"a"})
        result = minimize_breaking_set(deps("a"), oracle)
        assert [d.name for d in result.culprits] == ["a"]
        assert result.runs == 0
        assert not result.interaction

    @pytest.mark.parametrize("culprit", ["a", "b", "c", "d", "e"])
    def test_single_culprit_among_five(self, culprit: str) -> None:
        oracle = CountingOracle({culprit})
        result = minimize_breaking_set(deps("a", "b", "c", "d", "e"), oracle)
        assert [d.name for d in result.culprits] == [culprit]
        assert not result.interaction
        assert result.runs == len(oracle.calls)

    def test_single_culprit_among_two(self) -> None:
        oracle = CountingOracle({"b"})
        result = minimize_breaking_set(deps("a", "b"), oracle)
        assert [d.name for d in result.culprits] == ["b"]
        assert result.runs <= 2

    def test_large_set_stays_logarithmic_for_single_culprit(self) -> None:
        names = [f"dep{i:02d}" for i in range(32)]
        oracle = CountingOracle({"dep17"})
        result = minimize_breaking_set(deps(*names), oracle)
        assert [d.name for d in result.culprits] == ["dep17"]
        # ddmin on a single culprit is near binary search, far below n runs.
        assert result.runs <= 2 * 5 + 4

    def test_interaction_pair_flagged_experimental(self) -> None:
        oracle = CountingOracle({"a", "d"})
        result = minimize_breaking_set(deps("a", "b", "c", "d"), oracle)
        assert {d.name for d in result.culprits} == {"a", "d"}
        assert result.interaction

    def test_interaction_result_is_minimal(self) -> None:
        oracle = CountingOracle({"b", "c"})
        result = minimize_breaking_set(deps("a", "b", "c", "d", "e", "f"), oracle)
        assert {d.name for d in result.culprits} == {"b", "c"}

    def test_never_retests_cached_subsets(self) -> None:
        oracle = CountingOracle({"c"})
        result = minimize_breaking_set(deps("a", "b", "c", "d"), oracle)
        assert len(oracle.calls) == len(set(oracle.calls))
        assert result.runs == len(oracle.calls)

    def test_never_tests_full_or_empty_set(self) -> None:
        # Those are the baselines the CLI already ran.
        all_names = {"a", "b", "c", "d"}
        oracle = CountingOracle({"a"})
        minimize_breaking_set(deps(*sorted(all_names)), oracle)
        assert frozenset() not in oracle.calls
        assert frozenset(all_names) not in oracle.calls

    def test_trace_records_every_run(self) -> None:
        oracle = CountingOracle({"b"})
        result = minimize_breaking_set(deps("a", "b", "c"), oracle)
        assert len(result.trace) == result.runs


class TestVersionBisect:
    def test_two_versions_zero_runs(self) -> None:
        result = bisect_versions(["1.0", "2.0"], lambda v: pytest.fail("no oracle call"))
        assert result.last_passing == "1.0"
        assert result.first_failing == "2.0"
        assert result.runs == 0

    @pytest.mark.parametrize("break_at", [1, 2, 3, 4, 5])
    def test_boundary_found_anywhere(self, break_at: int) -> None:
        path = ["1.0", "1.1", "1.2", "1.3", "1.4", "2.0"]
        calls: list[str] = []

        def oracle(v: str) -> bool:
            calls.append(v)
            return path.index(v) < break_at

        result = bisect_versions(path, oracle)
        assert result.last_passing == path[break_at - 1]
        assert result.first_failing == path[break_at]
        assert result.runs == len(calls)

    def test_logarithmic_run_count(self) -> None:
        path = [f"1.{i}" for i in range(129)]  # 127 interior candidates
        result = bisect_versions(path, lambda v: int(v.split(".")[1]) < 100)
        assert result.runs <= 7

    def test_endpoints_never_retested(self) -> None:
        path = ["1.0", "1.5", "2.0"]
        calls: list[str] = []

        def oracle(v: str) -> bool:
            calls.append(v)
            return v == "1.0"

        bisect_versions(path, oracle)
        assert "1.0" not in calls
        assert "2.0" not in calls

    def test_too_short_path_rejected(self) -> None:
        with pytest.raises(ValueError):
            bisect_versions(["1.0"], lambda v: True)


class TestEstimates:
    def test_single_dep_no_candidates(self) -> None:
        lo, hi = estimate_runs(1, 2)
        assert lo == 2
        assert hi == 2

    def test_single_dep_with_candidates(self) -> None:
        lo, hi = estimate_runs(1, 6)  # 4 interior versions
        assert lo == 3
        assert hi == 2 + 3  # ceil(log2(5)) = 3

    def test_multiple_deps(self) -> None:
        lo, hi = estimate_runs(4, 2)
        assert lo == 3
        assert hi >= lo


class TestUnevaluableCandidates:
    """A candidate that will not install is not a candidate that failed.

    This is what makes an index-sourced candidate list safe: the index
    lists every published release, and some of them cannot be installed
    on the machine running the bisection. Reading "could not install" as
    "the test failed" makes the search blame a release it never ran.
    """

    def test_skipped_candidate_is_not_blamed(self) -> None:
        path = ["1.0", "1.1", "1.5", "2.0"]

        def oracle(version: str) -> bool | None:
            if version == "1.5":
                return None  # will not install here
            return version in ("1.0", "1.1")

        result = bisect_versions(path, oracle)
        assert (result.last_passing, result.first_failing) == ("1.1", "2.0")
        assert result.skipped == ["1.5"]

    def test_treating_it_as_failure_would_give_a_different_answer(self) -> None:
        # The same path with None collapsed to False, which is what the
        # two-valued oracle used to do. It blames 1.5, which never ran.
        path = ["1.0", "1.1", "1.5", "2.0"]

        def collapsed(version: str) -> bool:
            if version == "1.5":
                return False
            return version in ("1.0", "1.1")

        wrong = bisect_versions(path, collapsed)
        assert (wrong.last_passing, wrong.first_failing) == ("1.1", "1.5")
        assert wrong.skipped == []

    def test_probing_moves_outward_to_the_nearest_usable_candidate(self) -> None:
        path = ["1.0", "1.1", "1.2", "1.3", "2.0"]
        asked: list[str] = []

        def oracle(version: str) -> bool | None:
            asked.append(version)
            if version == "1.2":  # the midpoint of (0, 4)
                return None
            return version in ("1.0", "1.1")

        result = bisect_versions(path, oracle)
        assert asked[0] == "1.2"
        assert asked[1] == "1.3"  # the neighbour, not a restart at the start
        assert (result.last_passing, result.first_failing) == ("1.1", "1.3")
        assert result.skipped == ["1.2"]

    def test_every_candidate_unusable_falls_back_to_the_endpoints(self) -> None:
        path = ["1.0", "1.1", "1.2", "2.0"]
        result = bisect_versions(path, lambda v: None)
        assert (result.last_passing, result.first_failing) == ("1.0", "2.0")
        assert result.skipped == ["1.1", "1.2"]
        assert result.runs == 2  # each unusable candidate is tried once, not repeatedly

    def test_only_skips_inside_the_final_interval_are_reported(self) -> None:
        # A version ruled out early can end up outside the boundary, where
        # it is no longer a gap in the evidence.
        path = ["1.0", "1.1", "1.2", "1.3", "2.0"]

        def oracle(version: str) -> bool | None:
            if version == "1.2":
                return None
            return version == "1.1"

        result = bisect_versions(path, oracle)
        assert (result.last_passing, result.first_failing) == ("1.1", "1.3")
        assert result.skipped == ["1.2"]

    def test_trace_records_the_skip(self) -> None:
        path = ["1.0", "1.1", "2.0"]
        result = bisect_versions(path, lambda v: None)
        assert result.trace == [("1.1", None)]
