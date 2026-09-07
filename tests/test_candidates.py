"""Assembling the candidate path from local wheels and an index.

The index is always injected here (``fetch=``), so nothing in this file
opens a socket.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from depbisect.candidates import PLATFORM, PYTHON, build_candidates, filter_releases
from depbisect.index import DistFile, IndexError_, Release
from depbisect.tags import Tag

#: Stands in for a host that can install pure-Python wheels and nothing else.
PURE_PYTHON_HOST = frozenset({Tag("py3", "none", "any")})


def release(
    version: str,
    *,
    yanked: bool = False,
    requires: str | None = None,
    files: list[str] | None = None,
) -> Release:
    """A release whose files default to one universal wheel."""
    names = files if files is not None else [f"widget-{version}-py3-none-any.whl"]
    return Release(
        version=version,
        files=tuple(DistFile(name, yanked, requires) for name in names),
    )


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


def reasons(excluded: tuple[object, ...]) -> list[tuple[str, str]]:
    return [(item.version, item.reason) for item in excluded]  # type: ignore[attr-defined]


class TestFiltering:
    def test_prereleases_excluded_by_default(self) -> None:
        kept, excluded = filter_releases([release("1.1.0rc1"), release("1.2.0")])
        assert kept == ["1.2.0"]
        assert reasons(excluded) == [("1.1.0rc1", "pre-release")]

    def test_pre_flag_keeps_them(self) -> None:
        kept, excluded = filter_releases([release("1.1.0rc1")], allow_pre=True)
        assert kept == ["1.1.0rc1"]
        assert excluded == ()

    def test_yanked_excluded_by_default(self) -> None:
        kept, excluded = filter_releases([release("1.1.0", yanked=True)])
        assert kept == []
        assert reasons(excluded) == [("1.1.0", "yanked")]

    def test_include_yanked_keeps_them(self) -> None:
        kept, excluded = filter_releases([release("1.1.0", yanked=True)], allow_yanked=True)
        assert kept == ["1.1.0"]
        assert excluded == ()

    def test_incompatible_interpreter_excluded(self) -> None:
        kept, excluded = filter_releases(
            [release("1.1.0", requires=">=3.7,<3.11"), release("1.2.0", requires=">=3.8")],
            python=(3, 13, 0),
        )
        assert kept == ["1.2.0"]
        assert reasons(excluded) == [("1.1.0", PYTHON)]

    def test_the_exclusion_detail_names_the_specifier(self) -> None:
        _, excluded = filter_releases([release("1.1.0", requires=">=3.7,<3.11")], python=(3, 13, 0))
        assert excluded[0].detail == "Requires-Python >=3.7,<3.11"

    def test_a_release_lands_in_exactly_one_bucket(self) -> None:
        # A yanked pre-release must not be counted twice.
        kept, excluded = filter_releases([release("1.1.0rc1", yanked=True)])
        assert kept == []
        assert reasons(excluded) == [("1.1.0rc1", "pre-release")]

    def test_unparseable_version_is_kept_not_counted(self) -> None:
        kept, excluded = filter_releases([release("not-a-version")])
        assert kept == ["not-a-version"]
        assert excluded == ()

    def test_no_interpreter_given_means_no_compatibility_filter(self) -> None:
        kept, excluded = filter_releases([release("1.1.0", requires=">=3.99")])
        assert kept == ["1.1.0"]
        assert excluded == ()


class TestPlatformFiltering:
    """Releases with no distribution that could install here cost no probe."""

    def test_a_wheel_only_release_for_another_platform_is_excluded(self) -> None:
        kept, excluded = filter_releases(
            [release("1.1.0", files=["widget-1.1.0-cp38-cp38-win_amd64.whl"])],
            tags=PURE_PYTHON_HOST,
        )
        assert kept == []
        assert reasons(excluded) == [("1.1.0", PLATFORM)]
        assert excluded[0].detail == "no distribution for this platform"

    def test_an_sdist_only_release_is_kept(self) -> None:
        # An sdist may well build here. "No compatible wheel" and "not
        # installable" are different claims, and only the first is one
        # depbisect can make from a filename.
        kept, excluded = filter_releases(
            [release("1.1.0", files=["widget-1.1.0.tar.gz"])],
            tags=PURE_PYTHON_HOST,
        )
        assert kept == ["1.1.0"]
        assert excluded == ()

    def test_one_usable_wheel_among_many_keeps_the_release(self) -> None:
        kept, _ = filter_releases(
            [
                release(
                    "1.1.0",
                    files=[
                        "widget-1.1.0-cp38-cp38-win_amd64.whl",
                        "widget-1.1.0-cp313-cp313-manylinux_2_17_x86_64.whl",
                        "widget-1.1.0-py3-none-any.whl",
                    ],
                )
            ],
            tags=PURE_PYTHON_HOST,
        )
        assert kept == ["1.1.0"]

    def test_a_sdist_alongside_foreign_wheels_keeps_the_release(self) -> None:
        kept, _ = filter_releases(
            [
                release(
                    "1.1.0",
                    files=["widget-1.1.0-cp38-cp38-win_amd64.whl", "widget-1.1.0.tar.gz"],
                )
            ],
            tags=PURE_PYTHON_HOST,
        )
        assert kept == ["1.1.0"]

    def test_no_tag_set_means_no_platform_filtering(self) -> None:
        # tags=None is what an undescribable host produces, and it must
        # behave exactly as this filter did before it existed.
        kept, excluded = filter_releases(
            [release("1.1.0", files=["widget-1.1.0-cp38-cp38-win_amd64.whl"])],
            tags=None,
        )
        assert kept == ["1.1.0"]
        assert excluded == ()

    def test_an_unreadable_wheel_name_is_kept(self) -> None:
        kept, _ = filter_releases(
            [release("1.1.0", files=["widget-1.1.0-py3-none.whl"])],
            tags=PURE_PYTHON_HOST,
        )
        assert kept == ["1.1.0"]

    def test_requires_python_wins_the_bucket(self) -> None:
        # One release, two reasons: it is reported under the first check
        # that ruled it out, so the counts still sum to one per release.
        kept, excluded = filter_releases(
            [
                release(
                    "1.1.0",
                    requires="<3.9",
                    files=["widget-1.1.0-cp38-cp38-win_amd64.whl"],
                )
            ],
            python=(3, 13, 0),
            tags=PURE_PYTHON_HOST,
        )
        assert kept == []
        assert reasons(excluded) == [("1.1.0", PYTHON)]

    def test_only_the_files_this_interpreter_accepts_can_save_a_release(self) -> None:
        # The universal wheel is for an interpreter this host is not, so
        # the only file left is the Windows one: nothing to install.
        kept, excluded = filter_releases(
            [
                Release(
                    version="1.1.0",
                    files=(
                        DistFile("widget-1.1.0-py3-none-any.whl", False, "<3.9"),
                        DistFile("widget-1.1.0-cp38-cp38-win_amd64.whl", False, ">=3.13"),
                    ),
                )
            ],
            python=(3, 13, 0),
            tags=PURE_PYTHON_HOST,
        )
        assert kept == []
        assert reasons(excluded) == [("1.1.0", PLATFORM)]

    def test_the_path_and_counts_reflect_the_exclusion(self) -> None:
        result = build_candidates(
            "widget",
            "1.0.0",
            "2.0.0",
            online=True,
            tags=PURE_PYTHON_HOST,
            fetch=fetcher(
                [
                    release("1.1.0"),
                    release("1.5.0", files=["widget-1.5.0-cp38-cp38-win_amd64.whl"]),
                ]
            ),
        )
        assert result.path == ["1.0.0", "1.1.0", "2.0.0"]
        assert result.excluded_platform == 1
        assert result.describe_exclusions(platform="macosx_15_0_arm64") == (
            "Excluded from the index list: 1 with no distribution for this platform "
            "(macosx_15_0_arm64)"
        )

    def test_local_wheels_are_never_platform_filtered(self, tmp_path: Path) -> None:
        # The rule the whole module is built on: a file you pointed at is
        # a version you chose, whatever its tags say.
        d = tmp_path / "w"
        d.mkdir()
        (d / "widget-1.5.0-cp38-cp38-win_amd64.whl").write_bytes(b"")
        result = build_candidates("widget", "1.0.0", "2.0.0", find_links=[d], tags=PURE_PYTHON_HOST)
        assert result.path == ["1.0.0", "1.5.0", "2.0.0"]
        assert result.excluded == 0


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
