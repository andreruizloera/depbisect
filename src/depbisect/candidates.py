"""Assemble the candidate version path for one dependency.

This is where the two candidate sources meet: local distribution
directories (``--find-links``, always available, never networked) and a
package index (``--online``, opt-in). The result is the ordered path the
bisector will walk, plus an account of what was left out and why, so the
report can say what was searched instead of implying it searched
everything.

A release is dropped for one of four reasons: it is a pre-release, it is
yanked, its ``Requires-Python`` excludes the trial interpreter, or every
distribution it published is a wheel tagged for some other platform. The
first two have flags that put them back; the last two are facts about
the machine the trials will run on.

Filtering applies to INDEX candidates only. A wheel sitting in a
directory you pointed at is a version you chose deliberately, so
depbisect does not second-guess it; a release the index happens to list
is not. That also means behaviour with no ``--online`` is exactly what
it was before index support existed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from depbisect.index import DEFAULT_INDEX_URL, IndexError_, Release, fetch_releases
from depbisect.tags import Tag
from depbisect.versions import (
    VersionParseError,
    local_versions,
    parse_version,
    strictly_between,
    version_path,
)

Fetcher = Callable[..., list[Release]]

PRE = "pre-release"
YANKED = "yanked"
PYTHON = "python"
PLATFORM = "platform"


@dataclass(frozen=True)
class Excluded:
    """One release the index listed that the bisection will not walk.

    ``reason`` is one of the four constants above and decides the count
    it lands in; ``detail`` is the human sentence, which carries the
    specifier or the flag that would have kept the release.
    """

    version: str
    reason: str
    detail: str


@dataclass(frozen=True)
class CandidateSet:
    """The bisection path for one package and how it was arrived at."""

    package: str
    path: list[str]
    #: "index", "local", "index and local", or "none".
    source: str
    #: Index releases inside the interval that were filtered out, in the
    #: order the index listed them. Local candidates are never here.
    excluded_versions: tuple[Excluded, ...] = ()
    #: Set when the index was asked for and could not answer.
    note: str | None = None

    @property
    def interior(self) -> int:
        """Releases strictly between the good and bad versions."""
        return max(len(self.path) - 2, 0)

    @property
    def excluded(self) -> int:
        return len(self.excluded_versions)

    def _count(self, reason: str) -> int:
        return sum(1 for item in self.excluded_versions if item.reason == reason)

    @property
    def excluded_pre(self) -> int:
        return self._count(PRE)

    @property
    def excluded_yanked(self) -> int:
        return self._count(YANKED)

    @property
    def excluded_python(self) -> int:
        return self._count(PYTHON)

    @property
    def excluded_platform(self) -> int:
        return self._count(PLATFORM)

    def max_runs(self) -> int:
        """Worst-case oracle calls to bisect this path."""
        runs = 0
        while (1 << runs) < self.interior + 1:
            runs += 1
        return runs

    def describe_exclusions(
        self,
        python: tuple[int, ...] | None = None,
        platform: str | None = None,
    ) -> str | None:
        """One line naming what the index offered and we did not test."""
        if not self.excluded:
            return None
        parts = []
        if self.excluded_pre:
            parts.append(f"{self.excluded_pre} pre-release(s)")
        if self.excluded_yanked:
            parts.append(f"{self.excluded_yanked} yanked")
        if self.excluded_python:
            where = ""
            if python is not None:
                where = f" Python {'.'.join(str(p) for p in python)}"
            parts.append(
                f"{self.excluded_python} not compatible with{where or ' this interpreter'}"
            )
        if self.excluded_platform:
            where = f" ({platform})" if platform else ""
            parts.append(f"{self.excluded_platform} with no distribution for this platform{where}")
        return "Excluded from the index list: " + ", ".join(parts)


def build_candidates(
    package: str,
    good: str,
    bad: str,
    *,
    find_links: list[Path] | None = None,
    online: bool = False,
    index_url: str = DEFAULT_INDEX_URL,
    python: tuple[int, ...] = (),
    tags: frozenset[Tag] | None = None,
    allow_pre: bool = False,
    allow_yanked: bool = False,
    timeout: float = 30.0,
    fetch: Fetcher | None = None,
) -> CandidateSet:
    """Build the good-to-bad candidate path for one package.

    An index failure is never fatal here: it is recorded as a note and
    the local candidates are used instead, because half a search beats
    aborting a session that already knows which package broke.
    """
    local = local_versions(package, list(find_links or []))
    from_index: list[str] = []
    excluded: tuple[Excluded, ...] = ()
    note: str | None = None

    if online:
        # Resolved here rather than as a default argument so the module
        # attribute is what gets called, which is what tests replace.
        call = fetch or fetch_releases
        try:
            releases = call(package, index_url=index_url, timeout=timeout)
        except IndexError_ as exc:
            note = str(exc)
        else:
            # Restrict to the interval BEFORE filtering. An index lists a
            # project's whole history, and counting exclusions over all of
            # it would report releases that were never candidates here:
            # "7 releases are not compatible with this interpreter" reads
            # as a fact about this bisection, so it has to be one.
            in_range = [r for r in releases if strictly_between(r.version, good, bad)]
            from_index, excluded = filter_releases(
                in_range,
                python=python,
                tags=tags,
                allow_pre=allow_pre,
                allow_yanked=allow_yanked,
            )

    available = sorted(set(local) | set(from_index))
    try:
        path = version_path(good, bad, available)
    except VersionParseError:
        path = [good, bad]

    interior = set(path[1:-1])
    used_index = bool(interior & set(from_index))
    used_local = bool(interior & set(local))
    if used_index and used_local:
        source = "index and local"
    elif used_index:
        source = "index"
    elif used_local:
        source = "local"
    else:
        source = "none"

    return CandidateSet(
        package=package,
        path=path,
        source=source,
        excluded_versions=excluded,
        note=note,
    )


def filter_releases(
    releases: list[Release],
    *,
    python: tuple[int, ...] = (),
    tags: frozenset[Tag] | None = None,
    allow_pre: bool = False,
    allow_yanked: bool = False,
) -> tuple[list[str], tuple[Excluded, ...]]:
    """Drop releases a bisection should not spend a test run on.

    Returns the kept version strings and a record per dropped release.
    The order of the checks decides which bucket a release lands in, so
    a yanked pre-release is recorded once, as a pre-release.

    ``tags`` is the compatibility tag set of the interpreter trials will
    run under. None means the host could not be described, and then no
    release is dropped on tags at all: an unfiltered candidate costs one
    probe, a wrongly filtered one costs a real answer.

    A version string this cannot parse is KEPT. It cannot be ordered, so
    ``version_path`` will drop it anyway; guessing about it here would
    only make the exclusion counts wrong.
    """
    kept: list[str] = []
    excluded: list[Excluded] = []
    for release in releases:
        if not allow_pre and _is_prerelease(release.version):
            excluded.append(Excluded(release.version, PRE, "pre-release (--pre to include)"))
        elif not allow_yanked and release.yanked:
            excluded.append(
                Excluded(release.version, YANKED, "yanked (--include-yanked to include)")
            )
        elif python and not release.supports(python):
            excluded.append(
                Excluded(release.version, PYTHON, f"Requires-Python {_requires(release)}")
            )
        elif tags is not None and not release.installable_on(tags, python):
            excluded.append(
                Excluded(release.version, PLATFORM, "no distribution for this platform")
            )
        else:
            kept.append(release.version)
    return kept, tuple(excluded)


def _requires(release: Release) -> str:
    """The Requires-Python values a release declared, for a message."""
    stated = [spec for spec in release.requires_python if spec]
    return ", ".join(dict.fromkeys(stated)) if stated else "(none stated)"


def _is_prerelease(text: str) -> bool:
    try:
        return parse_version(text).phase_rank < 0
    except VersionParseError:
        return False
