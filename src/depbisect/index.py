"""Read a package index for the releases between two versions (opt-in).

This is the only part of depbisect that touches the network, and it is
off unless you pass ``--online``. It speaks the simple repository API,
so it works against PyPI and against any index that implements PEP 503
or PEP 691 (devpi, Artifactory, a private mirror).

Two response shapes are handled:

- PEP 691 JSON (``application/vnd.pypi.simple.v1+json``), which is what
  PyPI returns when asked for it. Preferred, because ``yanked`` and
  ``requires-python`` are real fields.
- PEP 503 HTML, the older anchor listing, where the same two facts live
  in ``data-yanked`` and ``data-requires-python`` attributes.

Everything here parses FILES and keeps them, because a version's
installability is a property of its files: a release counts as yanked
only when every one of its files is yanked, it supports an interpreter
when any one of its files does, and it is installable on this platform
when any one of its files is a wheel tagged for this host or an sdist
that might build here.

Only http and https URLs are accepted. Nothing is executed, nothing is
downloaded to disk, and the response is parsed with the standard library
(``json`` and ``html.parser``) exactly as text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse, urlsplit
from urllib.request import Request, urlopen

from depbisect import __version__
from depbisect.errors import DepbisectError
from depbisect.specifiers import python_supported
from depbisect.tags import Tag, wheel_is_compatible
from depbisect.versions import normalize_name, split_dist_filename

DEFAULT_INDEX_URL = "https://pypi.org/simple/"

_ACCEPT = (
    "application/vnd.pypi.simple.v1+json;q=1.0, "
    "application/vnd.pypi.simple.v1+html;q=0.2, "
    "text/html;q=0.01"
)


class IndexError_(DepbisectError):
    """The index could not be read. Named with a trailing underscore so it
    does not shadow the builtin ``IndexError``."""


@dataclass(frozen=True)
class DistFile:
    """One distribution file of one release, as the index listed it."""

    filename: str
    yanked: bool = False
    requires_python: str | None = None


@dataclass(frozen=True)
class Release:
    """One version of a package, together with its distribution files.

    A release holds its files rather than a summary of them because
    installability is a per-file fact: one usable file is enough, and
    "usable" means both the interpreter and the platform, which
    different files answer differently.
    """

    version: str
    files: tuple[DistFile, ...] = ()

    @property
    def yanked(self) -> bool:
        """Yanked only when every file of the release is yanked."""
        return bool(self.files) and all(file.yanked for file in self.files)

    @property
    def requires_python(self) -> tuple[str | None, ...]:
        """The distinct ``Requires-Python`` values across this release's files.

        None inside the tuple means a file that stated no constraint.
        """
        return tuple(dict.fromkeys(file.requires_python for file in self.files)) or (None,)

    def supports(self, python: tuple[int, ...]) -> bool:
        """True when at least one file of this release accepts ``python``.

        A release with several files can be partly compatible (an old
        wheel plus a newer sdist); one installable file is enough.
        """
        if not self.files:
            return True
        return any(python_supported(file.requires_python, python) for file in self.files)

    def installable_on(self, supported: frozenset[Tag], python: tuple[int, ...] = ()) -> bool:
        """True when some file is both for this interpreter and this platform.

        An sdist counts: it may build here, and depbisect has no way to
        know that it will not without trying. So does a wheel whose
        filename does not parse. A release fails this only when every
        file it has is a wheel whose tags rule this host out.
        """
        if not self.files:
            return True
        return any(
            (not python or python_supported(file.requires_python, python))
            and wheel_is_compatible(file.filename, supported)
            for file in self.files
        )


def fetch_releases(
    package: str,
    *,
    index_url: str = DEFAULT_INDEX_URL,
    timeout: float = 30.0,
    opener: object = None,
) -> list[Release]:
    """Fetch and parse every release of ``package`` from an index.

    ``opener`` exists so tests can drive the parsing without a socket;
    the default is ``urllib.request.urlopen``.
    """
    url = release_url(index_url, package)
    body, content_type = _get(url, timeout=timeout, opener=opener)
    return parse_index(body, package, content_type=content_type)


def release_url(index_url: str, package: str) -> str:
    """The simple-index URL for one project, with the scheme checked."""
    parsed = urlparse(index_url)
    if parsed.scheme not in ("http", "https"):
        raise IndexError_(
            f"index URL must be http or https, got {index_url!r}; "
            "depbisect will not read an index over any other scheme"
        )
    return f"{index_url.rstrip('/')}/{normalize_name(package)}/"


def _get(url: str, *, timeout: float, opener: object) -> tuple[bytes, str]:
    call = opener if opener is not None else urlopen
    # The scheme is checked in release_url, so this cannot open a file:// URL.
    request = Request(
        url,
        headers={"Accept": _ACCEPT, "User-Agent": f"depbisect/{__version__}"},
    )
    try:
        with call(request, timeout=timeout) as response:  # type: ignore[operator]
            body = response.read()
            content_type = response.headers.get("Content-Type", "") or ""
    except HTTPError as exc:
        if exc.code == 404:
            raise IndexError_(f"{url} returned 404: no such project at this index") from exc
        raise IndexError_(f"{url} returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise IndexError_(f"could not reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise IndexError_(f"timed out reading {url} after {timeout}s") from exc
    return body, content_type


def parse_index(body: bytes, package: str, *, content_type: str = "") -> list[Release]:
    """Parse a simple-index response, JSON or HTML, into releases."""
    if "json" in content_type.lower() or body.lstrip()[:1] == b"{":
        return _fold(_json_files(body), package)
    return _fold(_html_files(body), package)


def _json_files(body: bytes) -> list[DistFile]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise IndexError_(f"index response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        raise IndexError_("index JSON has no 'files' list; this is not a PEP 691 response")
    files: list[DistFile] = []
    for entry in payload["files"]:
        if not isinstance(entry, dict):
            continue
        filename = entry.get("filename")
        if not isinstance(filename, str):
            continue
        # PEP 691: yanked is false, true, or a string reason.
        yanked = entry.get("yanked", False)
        requires = entry.get("requires-python")
        files.append(
            DistFile(
                filename,
                yanked is not False and yanked is not None,
                requires if isinstance(requires, str) else None,
            )
        )
    return files


class _AnchorParser(HTMLParser):
    """Collect PEP 503 anchors: filename, data-yanked, data-requires-python."""

    def __init__(self) -> None:
        super().__init__()
        self.files: list[DistFile] = []
        self._pending: tuple[str | None, bool, str | None] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attributes = dict(attrs)
        href = attributes.get("href")
        yanked = "data-yanked" in attributes
        requires = attributes.get("data-requires-python")
        self._pending = (href, yanked, requires or None)
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._pending is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._pending is None:
            return
        href, yanked, requires = self._pending
        # PEP 503 says the anchor text is the filename; fall back to the
        # URL's last path segment for indexes that only get the href right.
        filename = "".join(self._text).strip() or _basename(href)
        if filename:
            self.files.append(DistFile(filename, yanked, requires))
        self._pending = None
        self._text = []


def _basename(href: str | None) -> str:
    if not href:
        return ""
    return unquote(urlsplit(href).path.rsplit("/", 1)[-1])


def _html_files(body: bytes) -> list[DistFile]:
    parser = _AnchorParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    parser.close()
    return parser.files


def _fold(files: list[DistFile], package: str) -> list[Release]:
    """Group this project's files by version, keeping each file intact."""
    target = normalize_name(package)
    versions: dict[str, list[DistFile]] = {}
    for entry in files:
        split = split_dist_filename(entry.filename)
        if split is None or split[0] != target:
            continue
        versions.setdefault(split[1], []).append(entry)
    return [Release(version=version, files=tuple(group)) for version, group in versions.items()]
