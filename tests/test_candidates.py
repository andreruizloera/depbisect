"""Assembling the candidate path from local wheels and an index.

The index is always injected here (``fetch=``), so nothing in this file
opens a socket.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from depbisect.candidates import (
    PLATFORM,
    PRE,
    PYTHON,
    build_candidates,
    default_index_url,
    filter_npm_releases,
    filter_releases,
)
from depbisect.index import DistFile, IndexError_, Release
from depbisect.npm import NpmHost, NpmRelease
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

    def test_a_node_package_reads_tarballs_and_not_wheels(
        self, tmp_path: Path, write_package
    ) -> None:
        d = tmp_path / "links"
        d.mkdir()
        (d / "widget-1.1.0-py3-none-any.whl").write_bytes(b"")
        write_package(d / "widget-1.2.0.tgz", "widget", "1.2.0")
        # Local pre-releases are never filtered, for Node exactly as for Python.
        write_package(d / "widget-1.3.0-rc.1.tgz", "widget", "1.3.0-rc.1")
        result = build_candidates("widget", "1.0.0", "2.0.0", ecosystem="node", find_links=[d])
        assert result.path == ["1.0.0", "1.2.0", "1.3.0-rc.1", "2.0.0"]
        assert result.source == "local"

    def test_a_python_package_never_reads_a_tarball(self, tmp_path: Path, write_package) -> None:
        d = tmp_path / "links"
        write_package(d / "widget-1.2.0.tgz", "widget", "1.2.0")
        result = build_candidates("widget", "1.0.0", "2.0.0", find_links=[d])
        assert result.path == ["1.0.0", "2.0.0"]
        assert result.source == "none"


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


MAC = NpmHost("darwin", "arm64")


def npm_fetcher(releases: list[NpmRelease]):
    def fetch(package: str, **kwargs: object) -> list[NpmRelease]:
        return releases

    return fetch


class TestNpmCandidates:
    """The same path assembly, fed by the npm registry for a Node project."""

    def test_registry_releases_become_candidates(self) -> None:
        def no_python_index(package: str, **kwargs: object) -> list[Release]:
            raise AssertionError("a Node package must not be looked up at the Python index")

        result = build_candidates(
            "widget",
            "1.0.0",
            "2.0.0",
            ecosystem="node",
            online=True,
            fetch=no_python_index,
            fetch_npm=npm_fetcher([NpmRelease("1.1.0"), NpmRelease("1.2.0"), NpmRelease("3.0.0")]),
        )
        assert result.path == ["1.0.0", "1.1.0", "1.2.0", "2.0.0"]
        assert result.source == "index"
        assert result.ecosystem == "node"

    def test_the_default_url_follows_the_ecosystem(self) -> None:
        seen: dict[str, object] = {}

        def fetch(package: str, **kwargs: object) -> list[NpmRelease]:
            seen.update(kwargs)
            return []

        build_candidates("widget", "1.0.0", "2.0.0", ecosystem="node", online=True, fetch_npm=fetch)
        assert seen["registry_url"] == "https://registry.npmjs.org/"
        assert default_index_url("python") == "https://pypi.org/simple/"

    def test_a_semver_hyphen_is_a_prerelease_whatever_follows_it(self) -> None:
        kept, excluded = filter_npm_releases(
            [NpmRelease("2.0.0-post.1"), NpmRelease("2.0.0-rc.1"), NpmRelease("2.0.1+build.5")]
        )
        assert kept == ["2.0.1+build.5"]
        assert reasons(excluded) == [("2.0.0-post.1", PRE), ("2.0.0-rc.1", PRE)]
        # PEP 440 reads the same string as a post-release and keeps it, which
        # is why the two ecosystems cannot share one pre-release rule.
        assert filter_releases([release("2.0.0-post.1")])[0] == ["2.0.0-post.1"]

    def test_pre_flag_keeps_them(self) -> None:
        kept, excluded = filter_npm_releases([NpmRelease("2.0.0-rc.1")], allow_pre=True)
        assert kept == ["2.0.0-rc.1"]
        assert excluded == ()

    def test_deprecated_releases_stay_candidates_and_are_named(self) -> None:
        # Deprecation is often an end-of-life notice on releases that still
        # install. Dropping them would delete candidates.
        result = build_candidates(
            "widget",
            "1.0.0",
            "2.0.0",
            ecosystem="node",
            online=True,
            fetch_npm=npm_fetcher(
                [NpmRelease("1.1.0", deprecated="no longer supported"), NpmRelease("1.2.0")]
            ),
        )
        assert result.path == ["1.0.0", "1.1.0", "1.2.0", "2.0.0"]
        assert result.deprecated == ("1.1.0",)
        assert result.excluded == 0

    def test_a_release_npm_refuses_on_this_platform_is_excluded(self) -> None:
        kept, excluded = filter_npm_releases(
            [NpmRelease("1.1.0", os=("win32",), cpu=("x64",)), NpmRelease("1.2.0", os=("darwin",))],
            host=MAC,
        )
        assert kept == ["1.2.0"]
        assert reasons(excluded) == [("1.1.0", PLATFORM)]
        assert excluded[0].detail == "not installable on this platform (os win32; cpu x64)"

    def test_no_host_means_no_platform_filtering(self) -> None:
        # host=None is what a machine without a readable Node produces.
        kept, excluded = filter_npm_releases([NpmRelease("1.1.0", os=("win32",))], host=None)
        assert kept == ["1.1.0"]
        assert excluded == ()

    def test_exclusions_count_the_interval_only(self) -> None:
        result = build_candidates(
            "widget",
            "1.0.0",
            "2.0.0",
            ecosystem="node",
            online=True,
            npm_host=MAC,
            fetch_npm=npm_fetcher(
                [
                    NpmRelease("0.9.0", os=("win32",)),  # older than good
                    NpmRelease("1.5.0", os=("win32",)),
                    NpmRelease("1.6.0-beta.1"),
                    NpmRelease("3.0.0-rc.1"),  # newer than bad
                ]
            ),
        )
        assert result.path == ["1.0.0", "2.0.0"]
        assert result.excluded_platform == 1
        assert result.excluded_pre == 1
        assert result.describe_exclusions(platform="darwin arm64") == (
            "Excluded from the registry list: 1 pre-release(s), "
            "1 not installable on this platform (darwin arm64)"
        )

    def test_a_registry_failure_degrades_to_a_note(self) -> None:
        def fetch(package: str, **kwargs: object) -> list[NpmRelease]:
            raise IndexError_("could not reach the registry")

        result = build_candidates(
            "widget", "1.0.0", "2.0.0", ecosystem="node", online=True, fetch_npm=fetch
        )
        assert result.note == "could not reach the registry"
        assert result.path == ["1.0.0", "2.0.0"]
        assert result.source == "none"
