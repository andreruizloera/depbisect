"""Version parsing, ordering, and candidate discovery.

depbisect has zero runtime dependencies, so it carries its own small
version comparator instead of pulling in ``packaging``. It understands
the common shapes of both PEP 440 versions (1.2.3, 1.2.3a1, 1.2.3rc1,
1.2.3.post1, 1.2.3.dev1, epochs) and npm-style semver (1.2.3,
1.2.3-beta.1, build metadata after ``+``). Exotic version strings that
do not parse raise VersionParseError and callers degrade gracefully:
you can still diff them as opaque strings, you just cannot order them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from depbisect.errors import DepbisectError


class VersionParseError(DepbisectError):
    """A version string we cannot order."""


_VERSION_RE = re.compile(r"^v?(?:(\d+)!)?(\d+(?:\.\d+)*)(.*)$")
_PHASE_RE = re.compile(
    r"^[-._]?(dev|alpha|a|beta|b|preview|pre|rc|c|post|rev|r)[-._]?(\d*)(.*)$",
    re.IGNORECASE,
)

# Ordering of release phases relative to the final release (rank 0).
_PHASE_RANK = {
    "dev": -4,
    "alpha": -3,
    "a": -3,
    "beta": -2,
    "b": -2,
    "preview": -1,
    "pre": -1,
    "rc": -1,
    "c": -1,
    "post": 1,
    "rev": 1,
    "r": 1,
}


@dataclass(frozen=True, order=True)
class Version:
    """A parsed version with a total order. ``text`` is the original string."""

    epoch: int
    release: tuple[int, ...]
    phase_rank: int
    phase_num: int
    tiebreak: str
    text: str = field(compare=False)

    def __str__(self) -> str:
        return self.text


def parse_version(text: str) -> Version:
    """Parse a PEP 440 or semver-flavored version string.

    Raises VersionParseError for strings with no leading numeric release.
    """
    raw = text.strip()
    # Semver build metadata never affects precedence.
    body = raw.split("+", 1)[0]
    m = _VERSION_RE.match(body)
    if not m or not m.group(2):
        raise VersionParseError(f"cannot parse version {text!r}")
    epoch = int(m.group(1)) if m.group(1) else 0
    release = tuple(int(p) for p in m.group(2).split("."))
    suffix = m.group(3)

    phase_rank = 0
    phase_num = 0
    tiebreak = ""
    if suffix:
        pm = _PHASE_RE.match(suffix)
        if pm:
            phase_rank = _PHASE_RANK[pm.group(1).lower()]
            phase_num = int(pm.group(2)) if pm.group(2) else 0
            tiebreak = pm.group(3).lower()
        else:
            # Unknown suffix (for example "-nightly.20240101"). Treat it
            # as a pre-release and fall back to a string tiebreak so the
            # order stays deterministic.
            phase_rank = -1
            tiebreak = suffix.lower()
    return Version(epoch, _pad(release), phase_rank, phase_num, tiebreak, raw)


def _pad(release: tuple[int, ...], width: int = 6) -> tuple[int, ...]:
    """Pad so that 1.0 == 1.0.0 for comparison purposes."""
    return release + (0,) * (width - len(release))


def compare(a: str, b: str) -> int:
    """Return -1, 0, or 1 comparing two version strings."""
    va, vb = parse_version(a), parse_version(b)
    if va < vb:
        return -1
    if vb < va:
        return 1
    return 0


def strictly_between(text: str, good: str, bad: str) -> bool:
    """Is ``text`` strictly inside the interval the bisection will walk?

    Order-insensitive, so it holds for a downgrade (bad older than good)
    too. An unparseable version is not between anything, because it
    cannot be placed on the path at all.
    """
    try:
        v, vg, vb = parse_version(text), parse_version(good), parse_version(bad)
    except VersionParseError:
        return False
    lo, hi = (vg, vb) if vg < vb else (vb, vg)
    return lo < v < hi


def version_path(good: str, bad: str, available: list[str]) -> list[str]:
    """Ordered list of versions to bisect over, from good to bad inclusive.

    ``available`` is any locally known version list; only versions
    strictly between good and bad are kept. Works for downgrades too:
    the path always starts at good and ends at bad.
    """
    vg, vb = parse_version(good), parse_version(bad)
    if vg == vb:
        return [good]
    lo, hi = (vg, vb) if vg < vb else (vb, vg)
    between: list[Version] = []
    seen = {vg, vb}
    for text in available:
        try:
            v = parse_version(text)
        except VersionParseError:
            continue
        if lo < v < hi and v not in seen:
            seen.add(v)
            between.append(v)
    between.sort()
    if vb < vg:
        between.reverse()
    return [good] + [v.text for v in between] + [bad]


_SDIST_SUFFIXES = (".tar.gz", ".zip", ".tar.bz2")


def normalize_name(name: str) -> str:
    """PEP 503 normalization: lowercase, collapse runs of -_. to a dash."""
    return re.sub(r"[-_.]+", "-", name).lower()


def split_dist_filename(filename: str) -> tuple[str, str] | None:
    """Split a wheel or sdist filename into (normalized name, version).

    Returns None for anything that is not a recognizable distribution
    filename, including signatures and metadata files. Wheels are
    ``name-version[-build]-py-abi-platform.whl``, where neither the name
    nor the version may contain a dash, so the first two dash-separated
    fields are exactly what we want. Sdists are ``name-version.suffix``
    and their names may contain dashes, so the version is split off the
    right.
    """
    if filename.endswith(".whl"):
        parts = filename[: -len(".whl")].split("-")
        if len(parts) < 5 or not parts[0] or not parts[1]:
            return None
        return normalize_name(parts[0]), parts[1]
    for suffix in _SDIST_SUFFIXES:
        if filename.endswith(suffix):
            stem = filename[: -len(suffix)]
            name, sep, version = stem.rpartition("-")
            if not sep or not name or not version:
                return None
            return normalize_name(name), version
    return None


def local_versions(package: str, find_links: list[Path]) -> list[str]:
    """Versions of ``package`` discoverable in local wheel/sdist directories.

    This candidate source never talks to the network. Filenames follow
    the standard dist naming convention (``name-version-...whl`` /
    ``name-version.tar.gz``).
    """
    target = normalize_name(package)
    found: set[str] = set()
    for directory in find_links:
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            split = split_dist_filename(entry.name)
            if split is None or split[0] != target:
                continue
            found.add(split[1])
    try:
        return sorted(found, key=parse_version)
    except VersionParseError:
        return sorted(found)
