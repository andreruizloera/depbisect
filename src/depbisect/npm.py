"""Read the npm registry for the releases between two versions (opt-in).

The Node half of ``--online``. Like index.py it is off unless you pass
``--online``, issues GET requests only, over http or https, and parses
the response as JSON text: nothing is executed and nothing is written
to disk.

It asks for the abbreviated package document
(``application/vnd.npm.install-v1+json``), the form npm itself installs
from, which carries every per-version fact a bisection needs: the
version, whether it is deprecated, and the ``os``, ``cpu`` and ``libc``
lists npm checks before it will install it.

Which of those facts decide candidacy follows what ``npm install``
enforces, read in npm-install-checks and arborist rather than assumed:

- ``os``, ``cpu`` and ``libc`` are enforced. A non-optional package whose
  lists rule the host out fails with EBADPLATFORM, so that release can
  never become a trial and is excluded before the search starts.
- ``engines`` is not. Arborist only warns on a mismatch unless
  ``engine-strict`` is set, so such a release installs and gets tested.
- ``deprecated`` is not. npm's version picker prefers a non-deprecated
  release when a RANGE is resolved, and installs a deprecated one when it
  is asked for by exact version, which is how trials ask. Deprecation is
  also routinely an end-of-life notice across a whole release line, so
  treating it like a yanked PyPI release would delete real candidates.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from urllib.parse import quote, urlparse

from depbisect.index import IndexError_, http_get

DEFAULT_REGISTRY_URL = "https://registry.npmjs.org/"

_ACCEPT = "application/vnd.npm.install-v1+json; q=1.0, application/json; q=0.8"


@dataclass(frozen=True)
class NpmHost:
    """The values npm compares a package's platform lists against.

    ``os`` and ``cpu`` are Node's ``process.platform`` and
    ``process.arch``. ``libc`` is ``glibc`` or ``musl`` on Linux and None
    anywhere else, or on a Linux whose C library npm cannot name.
    """

    os: str
    cpu: str
    libc: str | None = None

    def describe(self) -> str:
        parts = [self.os, self.cpu]
        if self.libc:
            parts.append(self.libc)
        return " ".join(parts)


@dataclass(frozen=True)
class NpmRelease:
    """One version of an npm package, as the registry listed it."""

    version: str
    deprecated: str | None = None
    os: tuple[str, ...] | None = None
    cpu: tuple[str, ...] | None = None
    libc: tuple[str, ...] | None = None

    def installable_on(self, host: NpmHost) -> bool:
        """npm-install-checks' ``checkPlatform``, without ``--force``.

        A missing list allows everything. A ``libc`` list on a host with
        no libc to compare (any host but Linux) rules the release out,
        because that is what npm does.
        """
        os_ok = self.os is None or _check_list(host.os, self.os)
        cpu_ok = self.cpu is None or _check_list(host.cpu, self.cpu)
        libc_ok = self.libc is None or (host.libc is not None and _check_list(host.libc, self.libc))
        return os_ok and cpu_ok and libc_ok

    def platform_requirements(self) -> str:
        """The platform lists this release declared, for a message."""
        stated = [
            f"{field} {','.join(values)}"
            for field, values in (("os", self.os), ("cpu", self.cpu), ("libc", self.libc))
            if values is not None
        ]
        return "; ".join(stated) or "(none stated)"


def _check_list(value: str, entries: tuple[str, ...]) -> bool:
    """npm-install-checks' ``checkList``, transcribed.

    Match none of the negated entries, and at least one of the plain
    ones if there are any. A lone ``any`` matches everything, and a list
    that is empty or entirely negated matches whatever it does not name.
    """
    if len(entries) == 1 and entries[0] == "any":
        return True
    negated = 0
    match = False
    for entry in entries:
        if entry.startswith("!"):
            negated += 1
            if value == entry[1:]:
                return False
        else:
            match = match or value == entry
    return match or negated == len(entries)


def fetch_npm_releases(
    package: str,
    *,
    registry_url: str = DEFAULT_REGISTRY_URL,
    timeout: float = 30.0,
    opener: object = None,
) -> list[NpmRelease]:
    """Fetch and parse every published version of ``package``.

    ``opener`` exists so tests can drive the parsing without a socket;
    the default is ``urllib.request.urlopen``.
    """
    url = package_url(registry_url, package)
    body, _ = http_get(
        url,
        timeout=timeout,
        opener=opener,
        accept=_ACCEPT,
        missing="no such package at this registry",
    )
    return parse_packument(body)


def package_url(registry_url: str, package: str) -> str:
    """The package document URL, with the scheme and the name checked.

    A scoped name keeps its ``@`` and has its slash escaped, which is the
    form npm requests (npm-package-arg's ``escapedName``).
    """
    parsed = urlparse(registry_url)
    if parsed.scheme not in ("http", "https"):
        raise IndexError_(
            f"registry URL must be http or https, got {registry_url!r}; "
            "depbisect will not read a registry over any other scheme"
        )
    # npm refuses to publish a name that needs URL encoding beyond the
    # scope slash, or one that starts with a period, so anything else
    # here did not come from a real lockfile and must not be allowed to
    # reshape the request path.
    segments = package.split("/")
    if (
        quote(package, safe="@/") != package
        or len(segments) > 2
        or any(not segment or segment.startswith(".") for segment in segments)
    ):
        raise IndexError_(f"{package!r} is not a valid npm package name")
    return f"{registry_url.rstrip('/')}/{package.replace('/', '%2f', 1)}"


def parse_packument(body: bytes) -> list[NpmRelease]:
    """Parse an abbreviated or full package document into releases."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise IndexError_(f"registry response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("versions"), dict):
        raise IndexError_(
            "registry JSON has no 'versions' object; this is not an npm package document"
        )
    releases: list[NpmRelease] = []
    for version, manifest in payload["versions"].items():
        if not isinstance(version, str) or not isinstance(manifest, dict):
            continue
        deprecated = manifest.get("deprecated")
        releases.append(
            NpmRelease(
                version=version,
                # `npm deprecate pkg@1.0.0 ""` is how a deprecation is
                # withdrawn, so an empty message means not deprecated.
                deprecated=deprecated if isinstance(deprecated, str) and deprecated else None,
                os=_platform_list(manifest.get("os")),
                cpu=_platform_list(manifest.get("cpu")),
                libc=_platform_list(manifest.get("libc")),
            )
        )
    return releases


def _platform_list(value: object) -> tuple[str, ...] | None:
    """Read an ``os``/``cpu``/``libc`` field the way npm's truthiness does.

    npm checks a field only when it is truthy, so an absent field or an
    empty string imposes nothing, while an empty ARRAY is truthy and is
    checked (and then matches everything). A lone string is a one-entry
    list.
    """
    if isinstance(value, str):
        return (value,) if value else None
    if isinstance(value, list):
        return tuple(entry for entry in value if isinstance(entry, str))
    return None


# npm-install-checks' current-env.js, transcribed so the host is described
# with the same values npm will compare against: process.platform and
# process.arch, and on Linux the C library family, read from /usr/bin/ldd
# first and from Node's diagnostic report only if that file is unreadable.
_HOST_SCRIPT = r"""
const fs = require('node:fs');
let libc = null;
if (process.platform === 'linux') {
  let family;
  try {
    const content = fs.readFileSync('/usr/bin/ldd', 'utf-8');
    family = content.includes('musl') ? 'musl'
      : content.includes('GNU C Library') ? 'glibc' : null;
  } catch {
    family = undefined;
  }
  if (family === undefined) {
    process.report.excludeNetwork = true;
    const report = process.report.getReport();
    const isMusl = (f) => f.includes('libc.musl-') || f.includes('ld-musl-');
    family = report.header && report.header.glibcVersionRuntime ? 'glibc'
      : Array.isArray(report.sharedObjects) && report.sharedObjects.some(isMusl) ? 'musl'
      : null;
  }
  libc = family;
}
console.log(JSON.stringify({ os: process.platform, cpu: process.arch, libc }));
"""


def host_platform(node: str = "node", timeout: float = 30.0) -> NpmHost | None:
    """Describe this host the way npm will, by asking Node.

    Trials run ``npm install`` from PATH, and that npm checks platforms
    against the Node that runs it, so the same ``node`` is asked. None
    when there is no Node to ask or its answer cannot be read; the
    caller then filters nothing on platform, because an unfiltered
    candidate costs one probe and a wrongly filtered one costs a real
    answer.
    """
    try:
        result = subprocess.run(
            [node, "-e", _HOST_SCRIPT],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    os_name, cpu, libc = data.get("os"), data.get("cpu"), data.get("libc")
    if not isinstance(os_name, str) or not isinstance(cpu, str) or not os_name or not cpu:
        return None
    return NpmHost(os_name, cpu, libc if isinstance(libc, str) and libc else None)
