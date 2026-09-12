"""npm registry reading: URLs, parsing, npm's platform rule, one real HTTP round trip.

Every test here is offline. The HTTP tests bind a throwaway server to
127.0.0.1, and the host tests run a stand-in for ``node``, except the one
that asks a real Node when there is one on PATH.
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from depbisect.index import IndexError_
from depbisect.npm import (
    NpmHost,
    NpmRelease,
    fetch_npm_releases,
    host_platform,
    package_url,
    parse_packument,
)

PACKUMENT = json.dumps(
    {
        "name": "widget",
        "dist-tags": {"latest": "2.0.0"},
        "modified": "2026-01-01T00:00:00.000Z",
        "versions": {
            "1.0.0": {"name": "widget", "version": "1.0.0"},
            "1.1.0": {"name": "widget", "version": "1.1.0", "deprecated": "use 2.x"},
            "1.2.0": {"name": "widget", "version": "1.2.0", "deprecated": ""},
            "1.3.0": {"name": "widget", "version": "1.3.0", "os": "win32", "cpu": ["x64", "arm64"]},
            "2.0.0-beta.1": {"name": "widget", "version": "2.0.0-beta.1"},
            "2.0.0": {"name": "widget", "version": "2.0.0", "libc": ["glibc"]},
        },
    }
).encode()

MAC = NpmHost("darwin", "arm64")
LINUX_GLIBC = NpmHost("linux", "x64", "glibc")
LINUX_MUSL = NpmHost("linux", "x64", "musl")


def by_version(releases: list[NpmRelease]) -> dict[str, NpmRelease]:
    return {release.version: release for release in releases}


class TestPackageUrl:
    def test_a_scoped_name_keeps_its_at_and_escapes_its_slash(self) -> None:
        # npm-package-arg's escapedName, which is the path npm requests.
        assert package_url("https://registry.npmjs.org/", "@types/node") == (
            "https://registry.npmjs.org/@types%2fnode"
        )

    def test_an_unscoped_name_without_a_trailing_slash(self) -> None:
        assert package_url("http://127.0.0.1:4873", "left-pad") == "http://127.0.0.1:4873/left-pad"

    @pytest.mark.parametrize("url", ["file:///etc", "ftp://example.test/"])
    def test_refuses_any_scheme_but_http(self, url: str) -> None:
        with pytest.raises(IndexError_, match="must be http or https"):
            package_url(url, "widget")

    @pytest.mark.parametrize("name", ["", "../admin", "a b", "widget?x=1", "@a/b/c"])
    def test_a_name_that_would_reshape_the_request_is_refused(self, name: str) -> None:
        # A lockfile npm wrote cannot contain any of these, so a name like
        # this is not a package and must not become part of a URL path.
        with pytest.raises(IndexError_, match="not a valid npm package name"):
            package_url("https://registry.npmjs.org/", name)


class TestParsing:
    def test_every_version_is_read(self) -> None:
        assert set(by_version(parse_packument(PACKUMENT))) == {
            "1.0.0",
            "1.1.0",
            "1.2.0",
            "1.3.0",
            "2.0.0-beta.1",
            "2.0.0",
        }

    def test_a_message_deprecates_and_an_empty_one_withdraws_it(self) -> None:
        releases = by_version(parse_packument(PACKUMENT))
        assert releases["1.1.0"].deprecated == "use 2.x"
        assert releases["1.2.0"].deprecated is None
        assert releases["1.0.0"].deprecated is None

    def test_platform_fields_as_a_string_or_a_list(self) -> None:
        releases = by_version(parse_packument(PACKUMENT))
        assert releases["1.3.0"].os == ("win32",)
        assert releases["1.3.0"].cpu == ("x64", "arm64")
        assert releases["2.0.0"].libc == ("glibc",)
        assert releases["1.0.0"].os is None

    def test_malformed_version_entries_are_skipped(self) -> None:
        body = json.dumps({"versions": {"1.0.0": "not an object", "1.1.0": {}}}).encode()
        assert [release.version for release in parse_packument(body)] == ["1.1.0"]

    def test_rejects_json_that_is_not_a_package_document(self) -> None:
        # The body the public registry sends with its 404.
        with pytest.raises(IndexError_, match="not an npm package document"):
            parse_packument(b'{"error": "Not found"}')

    def test_rejects_malformed_json(self) -> None:
        with pytest.raises(IndexError_, match="not valid JSON"):
            parse_packument(b"{oops")


class TestNpmPlatformRule:
    """npm-install-checks' ``checkPlatform``, one branch of ``checkList`` per row."""

    @pytest.mark.parametrize(
        ("listed", "expected"),
        [
            (None, True),  # no field: nothing to check
            (("darwin",), True),  # a plain entry names the host
            (("win32", "linux"), False),  # plain entries, none of them the host
            (("!win32",), True),  # every entry negated, none names the host
            (("!darwin",), False),  # a negated entry names the host
            (("darwin", "!darwin"), False),  # a negation beats a match
            (("any",), True),  # a lone "any"
            ((), True),  # an empty array: zero entries, all of them negated
        ],
    )
    def test_os_list(self, listed: tuple[str, ...] | None, expected: bool) -> None:
        assert NpmRelease("1.0.0", os=listed).installable_on(MAC) is expected

    def test_cpu_is_checked_the_same_way(self) -> None:
        assert NpmRelease("1.0.0", cpu=("x64",)).installable_on(MAC) is False
        assert NpmRelease("1.0.0", cpu=("arm64",)).installable_on(MAC) is True

    def test_a_libc_list_rules_out_a_host_with_no_libc(self) -> None:
        # npm: `if (target.libc && !libc) libcOk = false`. Off Linux there is
        # no libc to compare, so even a list that excludes only musl fails.
        assert NpmRelease("1.0.0", libc=("!musl",)).installable_on(MAC) is False

    def test_a_libc_list_on_linux(self) -> None:
        release = NpmRelease("1.0.0", libc=("glibc",))
        assert release.installable_on(LINUX_GLIBC) is True
        assert release.installable_on(LINUX_MUSL) is False

    def test_the_requirements_are_named_for_the_report(self) -> None:
        release = NpmRelease("1.3.0", os=("win32",), cpu=("x64", "arm64"))
        assert release.platform_requirements() == "os win32; cpu x64,arm64"


class _Handler(BaseHTTPRequestHandler):
    body = PACKUMENT
    status = 200
    seen_path: ClassVar[str] = ""
    seen_headers: ClassVar[dict[str, str]] = {}

    def do_GET(self) -> None:  # name fixed by http.server
        type(self).seen_path = self.path
        type(self).seen_headers = dict(self.headers)
        self.send_response(self.status)
        self.send_header("Content-Type", "application/vnd.npm.install-v1+json")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def registry():
    """A real HTTP registry on localhost. Offline, but a genuine round trip."""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()
    server.server_close()


class TestFetchOverHttp:
    def test_asks_for_the_abbreviated_document(self, registry: str) -> None:
        releases = by_version(fetch_npm_releases("widget", registry_url=registry))
        assert releases["1.3.0"].os == ("win32",)
        headers = {k.lower(): v for k, v in _Handler.seen_headers.items()}
        assert headers["accept"].startswith("application/vnd.npm.install-v1+json")
        assert headers["user-agent"].startswith("depbisect/")

    def test_a_scoped_package_is_requested_by_its_escaped_name(self, registry: str) -> None:
        fetch_npm_releases("@scope/widget", registry_url=registry)
        assert _Handler.seen_path == "/@scope%2fwidget"

    def test_404_names_the_registry(self, registry: str, monkeypatch) -> None:
        monkeypatch.setattr(_Handler, "status", 404)
        with pytest.raises(IndexError_, match="no such package at this registry"):
            fetch_npm_releases("widget", registry_url=registry)

    def test_unreachable_registry(self) -> None:
        # Port 1 on loopback: refused immediately, no network needed.
        with pytest.raises(IndexError_, match="could not reach"):
            fetch_npm_releases("widget", registry_url="http://127.0.0.1:1/", timeout=5)


def fake_node(tmp_path: Path, stdout: str, code: int = 0) -> str:
    """A stand-in ``node`` that prints ``stdout`` and exits with ``code``."""
    script = tmp_path / "node"
    script.write_text(f"#!/bin/sh\ncat <<'EOF'\n{stdout}\nEOF\nexit {code}\n")
    script.chmod(0o755)
    return str(script)


class TestHostPlatform:
    def test_reads_what_node_reports(self, tmp_path: Path) -> None:
        node = fake_node(tmp_path, '{"os": "linux", "cpu": "x64", "libc": "musl"}')
        assert host_platform(node) == NpmHost("linux", "x64", "musl")

    def test_no_libc_off_linux(self, tmp_path: Path) -> None:
        node = fake_node(tmp_path, '{"os": "darwin", "cpu": "arm64", "libc": null}')
        assert host_platform(node) == NpmHost("darwin", "arm64", None)

    @pytest.mark.parametrize(
        ("stdout", "code"),
        [("not json", 0), ('{"os": "linux"}', 0), ('{"os": "linux", "cpu": "x64"}', 1)],
    )
    def test_an_unreadable_answer_describes_nothing(
        self, tmp_path: Path, stdout: str, code: int
    ) -> None:
        # None means "filter nothing on platform", which is the safe
        # direction: a wrong description would delete real candidates.
        assert host_platform(fake_node(tmp_path, stdout, code)) is None

    def test_no_node_at_all(self, tmp_path: Path) -> None:
        assert host_platform(str(tmp_path / "no-such-node")) is None

    @pytest.mark.skipif(shutil.which("node") is None, reason="needs a real node on PATH")
    def test_a_real_node_agrees_with_this_process(self) -> None:
        host = host_platform()
        assert host is not None
        if sys.platform in ("darwin", "linux"):
            assert host.os == sys.platform
        if sys.platform != "linux":
            assert host.libc is None
