"""PEP 425 compatibility tags: which wheels this interpreter can install.

An index lists every distribution file a release published. Most of them
are wheels, and a wheel's filename states exactly which interpreters,
ABIs, and platforms it is for. That is enough to know, before spending a
test run, that a release has nothing installable here.

The tag set is computed from the interpreter depbisect is running under,
because that is the interpreter the trial virtualenv is created with
(``uv venv --python sys.executable`` in ``sandbox.py``). It is a fact
about the run, not a guess about the machine.

Two rules keep this from silently deleting real candidates, and both
match the rule ``specifiers.py`` already follows:

- UNKNOWN IS NOT NO. A filename that is not a wheel (an sdist, which may
  well build here), or a wheel whose tags will not parse, is kept.
- A HOST WE CANNOT DESCRIBE FILTERS NOTHING. ``host_tags`` returns None
  when it cannot work out the platform tags, and the caller then applies
  no tag filtering at all rather than a half-right one.

The supported set is deliberately a superset in the doubtful direction:
being too generous costs one probe that the three-valued oracle already
handles, while being too strict removes a release from the search with
no way for the user to notice.
"""

from __future__ import annotations

import os
import platform
import re
import sys
import sysconfig
from collections.abc import Sequence
from dataclasses import dataclass

#: manylinux aliases and the architectures each one was defined for.
_LEGACY_MANYLINUX = {
    (2, 5): ("manylinux1", ("x86_64", "i686")),
    (2, 12): ("manylinux2010", ("x86_64", "i686")),
    (2, 17): (
        "manylinux2014",
        ("x86_64", "i686", "aarch64", "armv7l", "ppc64", "ppc64le", "s390x"),
    ),
}

#: The binary formats a macOS wheel may be built for, per host arch.
_MAC_FORMATS = {
    "arm64": ("arm64", "universal2"),
    "x86_64": ("x86_64", "intel", "fat64", "fat32", "universal2", "universal"),
}

_GLIBC_RE = re.compile(r"(\d+)\.(\d+)")


@dataclass(frozen=True, order=True)
class Tag:
    """One PEP 425 tag triple, e.g. ``cp313-cp313-macosx_11_0_arm64``."""

    interpreter: str
    abi: str
    platform: str

    def __str__(self) -> str:
        return f"{self.interpreter}-{self.abi}-{self.platform}"


def parse_wheel_tags(filename: str) -> frozenset[Tag] | None:
    """Every tag a wheel filename declares, or None if it declares none.

    A wheel is ``name-version[-build]-python-abi-platform.whl`` and each
    of the last three fields may be a compressed set separated by dots,
    so ``py2.py3-none-any`` names two tags. None means "not a wheel we
    can read", which callers must treat as "no information", never as
    "incompatible".
    """
    if not filename.endswith(".whl"):
        return None
    parts = filename[: -len(".whl")].split("-")
    if len(parts) not in (5, 6):
        return None
    interpreters, abis, platforms = parts[-3:]
    if not interpreters or not abis or not platforms:
        return None
    tags = {
        Tag(interpreter, abi, plat)
        for interpreter in interpreters.split(".")
        for abi in abis.split(".")
        for plat in platforms.split(".")
        if interpreter and abi and plat
    }
    return frozenset(tags) or None


def wheel_is_compatible(filename: str, supported: frozenset[Tag]) -> bool:
    """Can this file be installed on a host with ``supported`` tags?

    True for anything that is not a readable wheel filename: an sdist can
    be built here, and a filename we cannot parse tells us nothing.
    """
    declared = parse_wheel_tags(filename)
    if declared is None:
        return True
    return not declared.isdisjoint(supported)


def supported_tags(
    *,
    python: tuple[int, int],
    platforms: Sequence[str],
    implementation: str = "cp",
    abis: Sequence[str] = (),
) -> frozenset[Tag]:
    """The tags an interpreter of this shape can install, as a set.

    Pure: everything about the host arrives as an argument, so the whole
    table is testable without being on the platform it describes.
    """
    major, minor = python
    interpreter = f"{implementation}{major}{minor}"
    plats = list(platforms)
    tags: set[Tag] = set()

    # Extension wheels built for exactly this interpreter and ABI.
    for abi in abis:
        tags.update(Tag(interpreter, abi, plat) for plat in plats)
    # A stable-ABI (abi3) wheel built for any earlier 3.x still loads here.
    for older in range(minor, 1, -1):
        older_interpreter = f"{implementation}{major}{older}"
        tags.update(Tag(older_interpreter, "abi3", plat) for plat in plats)
    # Platform-specific but ABI-less wheels, and the "any" platform.
    tags.update(Tag(interpreter, "none", plat) for plat in plats)
    tags.add(Tag(interpreter, "none", "any"))
    # Pure-Python wheels: py313, py3, and every earlier py3x.
    generic = [f"py{major}{minor}", f"py{major}"]
    generic += [f"py{major}{older}" for older in range(minor - 1, -1, -1)]
    for name in generic:
        tags.update(Tag(name, "none", plat) for plat in [*plats, "any"])
    return frozenset(tags)


def mac_platform_tags(version: tuple[int, int], machine: str) -> list[str]:
    """macOS platform tags a host at ``version`` can install, newest first.

    A wheel's macOS tag states its DEPLOYMENT TARGET, so a host accepts
    every target up to and including its own version and none above it:
    macosx_11_0_arm64 installs on macOS 15, macosx_16_0_arm64 does not.
    Since macOS 11 the minor number is not part of the compatibility
    story, but 10.x wheels still install, on Apple silicon as universal2
    binaries only (no arm64 wheel was ever built for a 10.x target).
    """
    major, minor = version
    formats = _MAC_FORMATS.get(machine, (machine,))
    out: list[str] = []
    if major >= 11:
        for host_major in range(major, 10, -1):
            out += [f"macosx_{host_major}_0_{fmt}" for fmt in formats]
        legacy = formats if machine == "x86_64" else ("universal2",)
        for legacy_minor in range(16, 3, -1):
            out += [f"macosx_10_{legacy_minor}_{fmt}" for fmt in legacy]
    else:
        for host_minor in range(minor, -1, -1):
            out += [f"macosx_{major}_{host_minor}_{fmt}" for fmt in formats]
    return out


def linux_platform_tags(machine: str, libc: tuple[str, int, int] | None) -> list[str] | None:
    """Linux platform tags, or None when the C library cannot be identified.

    manylinux and musllinux tags name the oldest C library they run
    against, so a host accepts every tag at or below its own version.
    Without knowing which library, and which version, this cannot be
    decided at all, and guessing would drop real candidates: the caller
    gets None and filters nothing.
    """
    if libc is None:
        return None
    family, major, minor = libc
    out = [f"linux_{machine}"]
    if family == "glibc":
        for older in range(minor, 4, -1):
            out.append(f"manylinux_{major}_{older}_{machine}")
            alias = _LEGACY_MANYLINUX.get((major, older))
            if alias is not None and machine in alias[1]:
                out.append(f"{alias[0]}_{machine}")
    elif family == "musl":
        for older in range(minor, -1, -1):
            out.append(f"musllinux_{major}_{older}_{machine}")
    else:
        return None
    return out


def windows_platform_tags(sysconfig_platform: str) -> list[str] | None:
    """Windows platform tags for a ``sysconfig.get_platform()`` value."""
    known = {"win32": ["win32"], "win-amd64": ["win_amd64"], "win-arm64": ["win_arm64"]}
    return known.get(sysconfig_platform)


@dataclass(frozen=True)
class HostTags:
    """The tag set of the interpreter trials will run under."""

    tags: frozenset[Tag]
    #: The most specific platform tag, for messages: "macosx_15_0_arm64".
    platform: str


def host_tags() -> HostTags | None:
    """Tags for the running interpreter, or None if the host is unclear.

    None is the safe answer: the caller then excludes nothing on tags and
    behaves exactly as it did before this filter existed.
    """
    platforms = _host_platform_tags()
    if not platforms:
        return None
    version = sys.version_info[:2]
    tags = supported_tags(
        python=version,
        platforms=platforms,
        implementation=_implementation_prefix(),
        abis=_host_abis(version),
    )
    return HostTags(tags=tags, platform=platforms[0])


def _host_platform_tags() -> list[str] | None:
    system = platform.system()
    if system == "Darwin":
        release = platform.mac_ver()[0]
        parts = release.split(".")
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            return None
        return mac_platform_tags((int(parts[0]), int(parts[1])), platform.machine())
    if system == "Linux":
        return linux_platform_tags(platform.machine(), _host_libc())
    if system == "Windows":
        return windows_platform_tags(sysconfig.get_platform())
    return None


def _host_libc() -> tuple[str, int, int] | None:
    """(family, major, minor) of the host C library, or None if unknown.

    glibc reports itself through confstr, and through the version string
    the standard library digs out of the interpreter binary. musl does
    neither reliably, so a musl host normally lands on None, which turns
    the tag filter off rather than mis-describing it.
    """
    try:
        reported = os.confstr("CS_GNU_LIBC_VERSION")
    except (ValueError, OSError, AttributeError):
        reported = None
    if not reported:
        name, version = platform.libc_ver()
        reported = f"{name} {version}" if name else ""
    if not reported:
        return None
    family = "glibc" if "glibc" in reported.lower() else "musl" if "musl" in reported else None
    if family is None:
        return None
    m = _GLIBC_RE.search(reported)
    if m is None:
        return None
    return family, int(m.group(1)), int(m.group(2))


def _implementation_prefix() -> str:
    return {"cpython": "cp", "pypy": "pp"}.get(sys.implementation.name, sys.implementation.name)


def _host_abis(version: tuple[int, int]) -> list[str]:
    """The ABI tags of this build, e.g. ["cp313"] or ["cp313t"] (free-threaded)."""
    soabi = sysconfig.get_config_var("SOABI")
    default = f"{_implementation_prefix()}{version[0]}{version[1]}"
    if isinstance(soabi, str):
        parts = soabi.split("-")
        if len(parts) >= 2 and parts[0] == "cpython" and parts[1]:
            return [f"cp{parts[1]}"]
    return [default]
