"""PEP 425 tag parsing and the compatibility table.

Every table test passes the host in explicitly, so the macOS cases run
on Linux and the Linux cases run on macOS. Only the last class asks the
real interpreter anything, and it asserts invariants that hold on any
host depbisect supports.
"""

from __future__ import annotations

import platform
import sys

import pytest

from depbisect.tags import (
    Tag,
    host_tags,
    linux_platform_tags,
    mac_platform_tags,
    parse_wheel_tags,
    supported_tags,
    wheel_is_compatible,
    windows_platform_tags,
)


class TestParseWheelTags:
    def test_single_tag(self) -> None:
        assert parse_wheel_tags("jinja2-3.1.4-py3-none-any.whl") == frozenset(
            {Tag("py3", "none", "any")}
        )

    def test_compressed_tag_set_expands_to_the_product(self) -> None:
        # A real six-tag wheel: two interpreters, one abi, three platforms.
        found = parse_wheel_tags(
            "cffi-1.17.1-py2.py3-none-macosx_10_9_x86_64.macosx_11_0_arm64.whl"
        )
        assert found == frozenset(
            {
                Tag("py2", "none", "macosx_10_9_x86_64"),
                Tag("py2", "none", "macosx_11_0_arm64"),
                Tag("py3", "none", "macosx_10_9_x86_64"),
                Tag("py3", "none", "macosx_11_0_arm64"),
            }
        )

    def test_build_tag_is_ignored(self) -> None:
        assert parse_wheel_tags("numpy-1.26.4-1-cp312-cp312-win_amd64.whl") == frozenset(
            {Tag("cp312", "cp312", "win_amd64")}
        )

    def test_abi3_wheel(self) -> None:
        assert parse_wheel_tags(
            "cryptography-42.0.5-cp37-abi3-manylinux_2_28_x86_64.whl"
        ) == frozenset({Tag("cp37", "abi3", "manylinux_2_28_x86_64")})

    def test_dual_manylinux_platform_tag(self) -> None:
        # manylinux wheels carry the modern and legacy spelling at once.
        found = parse_wheel_tags(
            "numpy-1.26.4-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
        )
        assert found == frozenset(
            {
                Tag("cp312", "cp312", "manylinux_2_17_x86_64"),
                Tag("cp312", "cp312", "manylinux2014_x86_64"),
            }
        )

    @pytest.mark.parametrize(
        "filename",
        [
            "jinja2-3.1.4.tar.gz",  # an sdist declares no tags at all
            "widget-1.0.0-py3-none.whl",  # too few fields
            "widget-1.0.0-1-2-py3-none-any.whl",  # too many fields
            "widget-1.0.0-py3--any.whl",  # empty abi field
            "widget-1.0.0-py3-none-any.whl.asc",  # a signature, not a wheel
            "",
        ],
    )
    def test_unreadable_filenames_declare_nothing(self, filename: str) -> None:
        assert parse_wheel_tags(filename) is None


class TestWheelIsCompatible:
    supported = frozenset({Tag("py3", "none", "any"), Tag("cp313", "cp313", "macosx_15_0_arm64")})

    def test_matching_wheel(self) -> None:
        assert wheel_is_compatible("widget-1.0.0-py3-none-any.whl", self.supported)

    def test_wheel_for_another_platform(self) -> None:
        assert not wheel_is_compatible("widget-1.0.0-cp38-cp38-win_amd64.whl", self.supported)

    def test_an_sdist_is_never_ruled_out(self) -> None:
        # It may build here, and no filename can say that it will not.
        assert wheel_is_compatible("widget-1.0.0.tar.gz", self.supported)

    def test_an_unreadable_wheel_name_is_never_ruled_out(self) -> None:
        assert wheel_is_compatible("widget-1.0.0-py3-none.whl", self.supported)


class TestSupportedTags:
    plain = supported_tags(python=(3, 13), platforms=["macosx_15_0_arm64"], abis=["cp313"])

    def test_universal_wheels(self) -> None:
        assert Tag("py3", "none", "any") in self.plain

    def test_this_interpreters_extension_wheels(self) -> None:
        assert Tag("cp313", "cp313", "macosx_15_0_arm64") in self.plain

    def test_another_interpreters_extension_wheels_are_not_supported(self) -> None:
        assert Tag("cp312", "cp312", "macosx_15_0_arm64") not in self.plain

    def test_abi3_wheels_built_for_an_older_cpython(self) -> None:
        # The stable ABI is forward compatible: a cp37-abi3 wheel loads
        # on 3.13, which is the whole point of building one.
        assert Tag("cp37", "abi3", "macosx_15_0_arm64") in self.plain

    def test_abi3_wheels_built_for_a_newer_cpython_are_not(self) -> None:
        assert Tag("cp314", "abi3", "macosx_15_0_arm64") not in self.plain

    def test_older_pure_python_wheels(self) -> None:
        assert Tag("py311", "none", "any") in self.plain

    def test_newer_pure_python_wheels_are_not_supported(self) -> None:
        assert Tag("py314", "none", "any") not in self.plain

    def test_free_threaded_abi(self) -> None:
        free = supported_tags(python=(3, 13), platforms=["linux_x86_64"], abis=["cp313t"])
        assert Tag("cp313", "cp313t", "linux_x86_64") in free
        assert Tag("cp313", "cp313", "linux_x86_64") not in free


class TestMacPlatformTags:
    arm = mac_platform_tags((15, 7), "arm64")

    def test_the_host_version_itself(self) -> None:
        assert "macosx_15_0_arm64" in self.arm

    def test_an_older_deployment_target_is_installable(self) -> None:
        assert "macosx_11_0_arm64" in self.arm

    def test_a_newer_deployment_target_is_not(self) -> None:
        # A wheel targeting macOS 16 will not load on a macOS 15 host,
        # which is the direction people get backwards.
        assert "macosx_16_0_arm64" not in self.arm

    def test_universal2_covers_the_ten_series_on_apple_silicon(self) -> None:
        assert "macosx_10_9_universal2" in self.arm

    def test_no_ten_series_arm64_wheels_exist(self) -> None:
        # Apple silicon starts at macOS 11, so a macosx_10_9_arm64 wheel
        # is not a thing and must not be treated as installable.
        assert "macosx_10_9_arm64" not in self.arm

    def test_intel_hosts_accept_the_intel_formats(self) -> None:
        intel = mac_platform_tags((14, 2), "x86_64")
        assert "macosx_10_9_x86_64" in intel
        assert "macosx_14_0_x86_64" in intel
        assert "macosx_10_9_intel" in intel
        assert "macosx_11_0_arm64" not in intel

    def test_a_ten_series_host(self) -> None:
        old = mac_platform_tags((10, 15), "x86_64")
        assert "macosx_10_15_x86_64" in old
        assert "macosx_10_9_x86_64" in old
        assert "macosx_11_0_x86_64" not in old


class TestLinuxPlatformTags:
    glibc = linux_platform_tags("x86_64", ("glibc", 2, 35))

    def test_the_bare_linux_tag(self) -> None:
        assert self.glibc is not None
        assert "linux_x86_64" in self.glibc

    def test_manylinux_at_or_below_the_host_glibc(self) -> None:
        assert self.glibc is not None
        assert "manylinux_2_35_x86_64" in self.glibc
        assert "manylinux_2_17_x86_64" in self.glibc

    def test_manylinux_above_the_host_glibc_is_not_installable(self) -> None:
        assert self.glibc is not None
        assert "manylinux_2_36_x86_64" not in self.glibc

    def test_legacy_aliases(self) -> None:
        assert self.glibc is not None
        assert "manylinux2014_x86_64" in self.glibc
        assert "manylinux2010_x86_64" in self.glibc
        assert "manylinux1_x86_64" in self.glibc

    def test_legacy_aliases_only_where_they_were_defined(self) -> None:
        arm = linux_platform_tags("aarch64", ("glibc", 2, 35))
        assert arm is not None
        assert "manylinux2014_aarch64" in arm
        assert "manylinux2010_aarch64" not in arm  # never existed for aarch64

    def test_musl_hosts_get_musllinux_and_not_manylinux(self) -> None:
        musl = linux_platform_tags("x86_64", ("musl", 1, 2))
        assert musl is not None
        assert "musllinux_1_2_x86_64" in musl
        assert "musllinux_1_1_x86_64" in musl
        assert "manylinux_2_17_x86_64" not in musl

    def test_an_unidentifiable_c_library_disables_the_filter(self) -> None:
        # None means "do not filter on tags at all", which is the safe
        # answer: a wrongly excluded release costs a real answer.
        assert linux_platform_tags("x86_64", None) is None


class TestWindowsPlatformTags:
    def test_known_platforms(self) -> None:
        assert windows_platform_tags("win-amd64") == ["win_amd64"]
        assert windows_platform_tags("win32") == ["win32"]
        assert windows_platform_tags("win-arm64") == ["win_arm64"]

    def test_unknown_platform_disables_the_filter(self) -> None:
        assert windows_platform_tags("win-ia64") is None


@pytest.mark.skipif(
    platform.system() not in ("Darwin", "Windows"),
    reason="only these two need no C library detection to be describable",
)
def test_a_mac_or_windows_host_is_always_describable() -> None:
    assert host_tags() is not None


@pytest.mark.skipif(
    host_tags() is None,
    reason="this host's platform tags cannot be determined, so it filters nothing",
)
class TestHostTags:
    """Invariants that must hold on whatever machine this suite runs on."""

    def test_universal_wheels_are_always_installable(self) -> None:
        host = host_tags()
        assert host is not None
        assert Tag("py3", "none", "any") in host.tags

    @pytest.mark.skipif(
        platform.system() == "Windows", reason="a Windows host can install a Windows wheel"
    )
    def test_a_windows_wheel_is_not_installable_on_a_posix_host(self) -> None:
        host = host_tags()
        assert host is not None
        assert not wheel_is_compatible("widget-1.0.0-cp38-cp38-win_amd64.whl", host.tags)

    def test_this_interpreters_own_wheels_are_installable(self) -> None:
        host = host_tags()
        assert host is not None
        version = f"{sys.version_info[0]}{sys.version_info[1]}"
        assert wheel_is_compatible(f"widget-1.0.0-py{version}-none-{host.platform}.whl", host.tags)

    def test_the_platform_label_is_the_most_specific_tag(self) -> None:
        host = host_tags()
        assert host is not None
        assert any(tag.platform == host.platform for tag in host.tags)
