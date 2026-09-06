"""Version parsing, ordering, and offline candidate discovery."""

from pathlib import Path

import pytest

from depbisect.versions import (
    VersionParseError,
    compare,
    local_versions,
    parse_version,
    split_dist_filename,
    strictly_between,
    version_path,
)


class TestParsing:
    def test_plain_release(self) -> None:
        v = parse_version("1.2.3")
        assert v.release[:3] == (1, 2, 3)
        assert v.phase_rank == 0

    def test_leading_v_and_whitespace(self) -> None:
        assert compare(" v1.2.3 ", "1.2.3") == 0

    def test_short_release_pads(self) -> None:
        assert compare("1.0", "1.0.0") == 0

    def test_epoch(self) -> None:
        assert compare("1!1.0", "999.0") == 1

    def test_build_metadata_ignored(self) -> None:
        assert compare("1.2.3+build.99", "1.2.3") == 0

    @pytest.mark.parametrize("bad", ["", "not-a-version", "abc.def", "==1.0"])
    def test_unparseable(self, bad: str) -> None:
        with pytest.raises(VersionParseError):
            parse_version(bad)


class TestOrdering:
    @pytest.mark.parametrize(
        ("lower", "higher"),
        [
            ("1.0.0", "1.0.1"),
            ("1.9.0", "1.10.0"),
            ("0.27.1", "0.28.0"),
            ("1.0.0", "2.0.0"),
            # PEP 440 phases
            ("1.0.0.dev1", "1.0.0a1"),
            ("1.0.0a1", "1.0.0b1"),
            ("1.0.0b2", "1.0.0rc1"),
            ("1.0.0rc1", "1.0.0"),
            ("1.0.0", "1.0.0.post1"),
            ("1.0.0rc1", "1.0.0rc2"),
            # semver prerelease forms
            ("1.0.0-alpha", "1.0.0-beta"),
            ("1.0.0-beta.1", "1.0.0"),
            ("1.0.0-rc.1", "1.0.0"),
            # unknown suffixes still order deterministically as prereleases
            ("1.0.0-nightly", "1.0.0"),
        ],
    )
    def test_pairs(self, lower: str, higher: str) -> None:
        assert compare(lower, higher) == -1
        assert compare(higher, lower) == 1


class TestVersionPath:
    def test_no_intermediates(self) -> None:
        assert version_path("1.0.0", "2.0.0", []) == ["1.0.0", "2.0.0"]

    def test_intermediates_sorted_and_bounded(self) -> None:
        available = ["0.9.0", "1.1.0", "1.10.0", "1.2.0", "2.0.0", "2.1.0"]
        assert version_path("1.0.0", "2.0.0", available) == [
            "1.0.0",
            "1.1.0",
            "1.2.0",
            "1.10.0",
            "2.0.0",
        ]

    def test_downgrade_direction(self) -> None:
        # A downgrade regression still bisects from good to bad.
        assert version_path("2.0.0", "1.0.0", ["1.5.0"]) == ["2.0.0", "1.5.0", "1.0.0"]

    def test_duplicates_and_junk_skipped(self) -> None:
        available = ["1.1.0", "1.1.0", "garbage", "1.0.0", "2.0.0"]
        assert version_path("1.0.0", "2.0.0", available) == ["1.0.0", "1.1.0", "2.0.0"]

    def test_equal_endpoints(self) -> None:
        assert version_path("1.0.0", "1.0.0", ["9.9.9"]) == ["1.0.0"]


class TestLocalVersions:
    def test_scans_wheels_and_sdists(self, tmp_path: Path) -> None:
        for name in [
            "brokenlib-1.0.0-py3-none-any.whl",
            "brokenlib-1.1.0-py3-none-any.whl",
            "brokenlib-2.0.0.tar.gz",
            "Broken_Lib-3.0.0-py3-none-any.whl",  # normalizes to broken-lib, not brokenlib
            "otherpkg-9.0.0-py3-none-any.whl",
            "README.txt",
        ]:
            (tmp_path / name).write_bytes(b"")
        assert local_versions("brokenlib", [tmp_path]) == ["1.0.0", "1.1.0", "2.0.0"]

    def test_name_normalization(self, tmp_path: Path) -> None:
        (tmp_path / "My_Pkg-1.0.0-py3-none-any.whl").write_bytes(b"")
        assert local_versions("my.pkg", [tmp_path]) == ["1.0.0"]
        assert local_versions("my-pkg", [tmp_path]) == ["1.0.0"]

    def test_missing_dir_is_empty(self, tmp_path: Path) -> None:
        assert local_versions("x", [tmp_path / "nope"]) == []

    def test_binary_wheel_tags(self, tmp_path: Path) -> None:
        (tmp_path / "fastpkg-1.2.0-cp313-cp313-macosx_11_0_arm64.whl").write_bytes(b"")
        assert local_versions("fastpkg", [tmp_path]) == ["1.2.0"]


class TestSplitDistFilename:
    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("widget-1.0.0-py3-none-any.whl", ("widget", "1.0.0")),
            ("widget-1.0.0-cp313-cp313-macosx_11_0_arm64.whl", ("widget", "1.0.0")),
            ("widget-1.0.0-1-py3-none-any.whl", ("widget", "1.0.0")),  # build tag
            ("Widget_Thing-2.0-py3-none-any.whl", ("widget-thing", "2.0")),
            ("widget-1.0.0.tar.gz", ("widget", "1.0.0")),
            ("some-dashed-name-1.0.0.tar.gz", ("some-dashed-name", "1.0.0")),
            ("widget-1.0.0.zip", ("widget", "1.0.0")),
            ("widget-1!2.0.0-py3-none-any.whl", ("widget", "1!2.0.0")),
        ],
    )
    def test_recognized(self, filename: str, expected: tuple[str, str]) -> None:
        assert split_dist_filename(filename) == expected

    @pytest.mark.parametrize(
        "filename",
        [
            "widget-1.0.0-py3-none-any.whl.asc",  # signature
            "widget-1.0.0-py3-none.whl",  # too few wheel tags
            "index.html",
            "widget.tar.gz",  # no version to split off
            "",
        ],
    )
    def test_not_a_distribution(self, filename: str) -> None:
        assert split_dist_filename(filename) is None


class TestStrictlyBetween:
    def test_interior_only(self) -> None:
        assert strictly_between("1.5.0", "1.0.0", "2.0.0")
        assert not strictly_between("1.0.0", "1.0.0", "2.0.0")  # endpoints are not interior
        assert not strictly_between("2.0.0", "1.0.0", "2.0.0")
        assert not strictly_between("3.0.0", "1.0.0", "2.0.0")

    def test_order_of_the_endpoints_does_not_matter(self) -> None:
        # A downgrade is still an interval.
        assert strictly_between("1.5.0", "2.0.0", "1.0.0")

    def test_unparseable_is_not_between_anything(self) -> None:
        assert not strictly_between("not-a-version", "1.0.0", "2.0.0")
