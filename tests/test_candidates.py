"""Assembling the candidate path from local wheels and an index.

The index is always injected here (``fetch=``), so nothing in this file
opens a socket.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from depbisect.candidates import build_candidates, filter_releases
from depbisect.index import IndexError_, Release


def release(version: str, *, yanked: bool = False, requires: str | None = None) -> Release:
    return Release(version=version, yanked=yanked, requires_python=(requires,))


def fetcher(releases: list[Release]):
    def fetch(package: str, **kwargs: object) -> list[Release]:
        return releases

    return fetch


def exploding(exc: Exception):
    def fetch(package: str, **kwargs: object) -> list[Release]:
        raise exc

    return fetch


@pytest.fixture
def wheels(tmp_path: Path) -> Path:
    d = tmp_path / "wheels"
    d.mkdir()
    for name in [
        "widget-1.0.0-py3-none-any.whl",
        "widget-1.1.0-py3-none-any.whl",
        "widget-2.0.0-py3-none-any.whl",
    ]:
        (d / name).write_bytes(b"")
    return d


class TestOfflineIsUnchanged:
    """Without --online nothing may differ from the pre-index behaviour."""

    def test_local_wheels_only(self, wheels: Path) -> None:
        result = build_candidates("widget", "1.0.0", "2.0.0", find_links=[wheels])
        assert result.path == ["1.0.0", "1.1.0", "2.0.0"]
        assert result.source == "local"
        assert result.excluded == 0

    def test_no_sources_at_all(self) -> None:
        result = build_candidates("widget", "1.0.0", "2.0.0")
        assert result.path == ["1.0.0", "2.0.0"]
        assert result.source == "none"
        assert result.interior == 0

    def test_local_prereleases_are_never_filtered(self, tmp_path: Path) -> None:
        # A wheel you put in a directory is a version you chose. Only the
        # index's own listing gets second-guessed.
        d = tmp_path / "w"
        d.mkdir()
        (d / "widget-1.5.0rc1-py3-none-any.whl").write_bytes(b"")
        result = build_candidates("widget", "1.0.0", "2.0.0", find_links=[d])
        assert result.path == ["1.0.0", "1.5.0rc1", "2.0.0"]

    def test_fetcher_is_not_called(self) -> None:
        def explode(package: str, **kwargs: object) -> list[Release]:
            raise AssertionError("the index must not be touched without online=True")

        build_candidates("widget", "1.0.0", "2.0.0", fetch=explode)


class TestOnline:
    def test_index_releases_become_candidates(self) -> None:
        result = build_candidates(
            "widget",
            "1.0.0",
            "2.0.0",
            online=True,
            fetch=fetcher([release("1.1.0"), release("1.2.0"), release("3.0.0")]),
        )
        assert result.path == ["1.0.0", "1.1.0", "1.2.0", "2.0.0"]
        assert result.source == "index"

    def test_index_and_local_are_merged_and_deduplicated(self, wheels: Path) -> None:
        result = build_candidates(
            "widget",
            "1.0.0",
            "2.0.0",
            find_links=[wheels],
            online=True,
            fetch=fetcher([release("1.1.0"), release("1.5.0")]),
        )
        assert result.path == ["1.0.0", "1.1.0", "1.5.0", "2.0.0"]
        assert result.source == "index and local"

    def test_downgrade_direction_is_preserved(self) -> None:
        result = build_candidates(
            "widget",
            "2.0.0",
            "1.0.0",
            online=True,
            fetch=fetcher([release("1.5.0")]),
        )
        assert result.path == ["2.0.0", "1.5.0", "1.0.0"]


class TestExclusionsCoverTheIntervalOnly:
    """An index lists a project's whole history; only the interval counts.

    Reporting "7 releases are not compatible with this interpreter" when
    5 of them were never between the good and bad versions makes the
    number a fact about the project rather than about this bisection.
    """

    def test_out_of_range_releases_are_not_counted(self) -> None:
        result = build_candidates(
            "widget",
            "1.0.0",
            "2.0.0",
            online=True,
            python=(3, 13, 0),
            fetch=fetcher(
                [
                    release("0.1.0", requires="<3.9"),  # older than good
                    release("9.9.9", requires="<3.9"),  # newer than bad
                    release("1.5.0", requires="<3.9"),  # actually in range
                ]
            ),
        )
        assert result.excluded_python == 1
        assert result.path == ["1.0.0", "2.0.0"]

    def test_endpoints_themselves_are_never_excluded(self) -> None:
        # The good and bad versions come from the manifest; the index has
        # no standing to remove them from their own bisection.
        result = build_candidates(
            "widget",
            "1.0.0rc1",
            "2.0.0",
            online=True,
            fetch=fetcher([release("1.0.0rc1"), release("2.0.0")]),
        )
        assert result.path == ["1.0.0rc1", "2.0.0"]
        assert result.excluded_pre == 0


class TestFiltering:
    def test_prereleases_excluded_by_default(self) -> None:
        kept, pre, yanked, incompatible = filter_releases([release("1.1.0rc1"), release("1.2.0")])
        assert kept == ["1.2.0"]
        assert (pre, yanked, incompatible) == (1, 0, 0)

    def test_pre_flag_keeps_them(self) -> None:
        kept, pre, _, _ = filter_releases([release("1.1.0rc1")], allow_pre=True)
        assert kept == ["1.1.0rc1"]
        assert pre == 0

    def test_yanked_excluded_by_default(self) -> None:
        kept, _, yanked, _ = filter_releases([release("1.1.0", yanked=True)])
        assert kept == []
        assert yanked == 1

    def test_include_yanked_keeps_them(self) -> None:
        kept, _, yanked, _ = filter_releases([release("1.1.0", yanked=True)], allow_yanked=True)
        assert kept == ["1.1.0"]
        assert yanked == 0

    def test_incompatible_interpreter_excluded(self) -> None:
        kept, _, _, incompatible = filter_releases(
            [release("1.1.0", requires=">=3.7,<3.11"), release("1.2.0", requires=">=3.8")],
            python=(3, 13, 0),
        )
        assert kept == ["1.2.0"]
        assert incompatible == 1

    def test_a_release_lands_in_exactly_one_bucket(self) -> None:
        # A yanked pre-release must not be counted twice.
        kept, pre, yanked, incompatible = filter_releases([release("1.1.0rc1", yanked=True)])
        assert kept == []
        assert (pre, yanked, incompatible) == (1, 0, 0)

    def test_unparseable_version_is_kept_not_counted(self) -> None:
        kept, pre, yanked, incompatible = filter_releases([release("not-a-version")])
        assert kept == ["not-a-version"]
        assert (pre, yanked, incompatible) == (0, 0, 0)

    def test_no_interpreter_given_means_no_compatibility_filter(self) -> None:
        kept, _, _, incompatible = filter_releases([release("1.1.0", requires=">=3.99")])
        assert kept == ["1.1.0"]
        assert incompatible == 0


class TestIndexFailureDegrades:
    def test_failure_is_recorded_and_local_candidates_survive(self, wheels: Path) -> None:
        result = build_candidates(
            "widget",
            "1.0.0",
            "2.0.0",
            find_links=[wheels],
            online=True,
            fetch=exploding(IndexError_("could not reach the index")),
        )
        assert result.note == "could not reach the index"
        assert result.path == ["1.0.0", "1.1.0", "2.0.0"]
        assert result.source == "local"

    def test_failure_with_nothing_local_still_returns_the_endpoints(self) -> None:
        result = build_candidates(
            "widget",
            "1.0.0",
            "2.0.0",
            online=True,
            fetch=exploding(IndexError_("boom")),
        )
        assert result.path == ["1.0.0", "2.0.0"]
        assert result.source == "none"


class TestRunEstimate:
    @pytest.mark.parametrize(
        ("interior", "expected"),
        [(0, 0), (1, 1), (2, 2), (3, 2), (4, 3), (7, 3), (8, 4)],
    )
    def test_max_runs_is_ceil_log2(self, interior: int, expected: int) -> None:
        versions = [f"1.0.{i + 1}" for i in range(interior)]
        result = build_candidates(
            "widget",
            "1.0.0",
            "2.0.0",
            online=True,
            fetch=fetcher([release(v) for v in versions]),
        )
        assert result.interior == interior
        assert result.max_runs() == expected
