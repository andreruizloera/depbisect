"""Simple-index reading: parsing, folding, and one real HTTP round trip.

Every test here is offline. The HTTP tests bind a throwaway server to
127.0.0.1 so the actual urllib request, headers, status handling, and
content-type negotiation are exercised without reaching any network.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from depbisect.index import (
    IndexError_,
    Release,
    fetch_releases,
    parse_index,
    release_url,
)

JSON_BODY = json.dumps(
    {
        "meta": {"api-version": "1.0"},
        "name": "widget",
        "files": [
            {"filename": "widget-1.0.0-py3-none-any.whl", "requires-python": ">=3.8"},
            {"filename": "widget-1.0.0.tar.gz", "requires-python": ">=3.8"},
            {"filename": "widget-1.1.0-py3-none-any.whl", "yanked": "broken sdist"},
            {"filename": "widget-1.2.0rc1-py3-none-any.whl"},
            {"filename": "widget-2.0.0-py3-none-any.whl", "requires-python": ">=3.12"},
            {"filename": "widget-2.0.0-py3-none-any.whl.asc"},  # signature, not a dist
            {"filename": "other-9.9.9-py3-none-any.whl"},  # different project
        ],
    }
).encode()

HTML_BODY = b"""<!DOCTYPE html>
<html><body>
  <a href="../../packages/widget-1.0.0-py3-none-any.whl#sha256=aa"
     data-requires-python="&gt;=3.8">widget-1.0.0-py3-none-any.whl</a><br/>
  <a href="../../packages/widget-1.1.0-py3-none-any.whl"
     data-yanked="broken sdist">widget-1.1.0-py3-none-any.whl</a><br/>
  <a href="../../packages/widget-2.0.0-py3-none-any.whl"
     data-requires-python="&gt;=3.12">widget-2.0.0-py3-none-any.whl</a><br/>
</body></html>
"""


def by_version(releases: list[Release]) -> dict[str, Release]:
    return {release.version: release for release in releases}


class TestReleaseUrl:
    def test_normalizes_the_project_name(self) -> None:
        assert release_url("https://pypi.org/simple/", "Zope.Interface") == (
            "https://pypi.org/simple/zope-interface/"
        )

    def test_tolerates_a_missing_trailing_slash(self) -> None:
        assert release_url("https://example.test/simple", "a") == "https://example.test/simple/a/"

    @pytest.mark.parametrize("url", ["file:///etc", "ftp://example.test/simple", "/simple"])
    def test_refuses_any_scheme_but_http(self, url: str) -> None:
        # depbisect reads an index over the network or not at all; a
        # file:// "index" would be a silent local-file read.
        with pytest.raises(IndexError_, match="must be http or https"):
            release_url(url, "widget")


class TestJsonParsing:
    def test_versions_and_metadata(self) -> None:
        releases = by_version(parse_index(JSON_BODY, "widget", content_type="application/json"))
        assert set(releases) == {"1.0.0", "1.1.0", "1.2.0rc1", "2.0.0"}
        assert releases["1.0.0"].requires_python == (">=3.8",)
        assert releases["1.2.0rc1"].requires_python == (None,)

    def test_yanked_reason_string_counts_as_yanked(self) -> None:
        releases = by_version(parse_index(JSON_BODY, "widget", content_type="application/json"))
        assert releases["1.1.0"].yanked is True
        assert releases["1.0.0"].yanked is False

    def test_other_projects_and_non_distributions_are_ignored(self) -> None:
        releases = by_version(parse_index(JSON_BODY, "widget", content_type="application/json"))
        assert "9.9.9" not in releases

    def test_content_type_is_not_required_to_detect_json(self) -> None:
        # Some indexes answer JSON with a bare text/plain content type.
        assert by_version(parse_index(JSON_BODY, "widget"))

    def test_rejects_json_that_is_not_a_simple_response(self) -> None:
        with pytest.raises(IndexError_, match="not a PEP 691 response"):
            parse_index(b'{"hello": 1}', "widget", content_type="application/json")

    def test_rejects_malformed_json(self) -> None:
        with pytest.raises(IndexError_, match="not valid JSON"):
            parse_index(b"{oops", "widget", content_type="application/json")


class TestHtmlParsing:
    def test_reads_data_attributes(self) -> None:
        releases = by_version(parse_index(HTML_BODY, "widget", content_type="text/html"))
        assert set(releases) == {"1.0.0", "1.1.0", "2.0.0"}
        assert releases["1.0.0"].requires_python == (">=3.8",)
        assert releases["1.1.0"].yanked is True
        assert releases["2.0.0"].yanked is False

    def test_falls_back_to_the_href_when_the_anchor_has_no_text(self) -> None:
        body = b'<a href="../../packages/widget-3.0.0-py3-none-any.whl#sha256=ff"></a>'
        releases = by_version(parse_index(body, "widget", content_type="text/html"))
        assert set(releases) == {"3.0.0"}

    def test_percent_encoded_filenames(self) -> None:
        body = b'<a href="/packages/widget-4.0.0-py3-none-any.whl%23x"></a>'
        assert parse_index(body, "widget", content_type="text/html") == []


class TestFolding:
    """A release is the fold of its files, and the fold has to be right."""

    def test_yanked_only_when_every_file_is_yanked(self) -> None:
        body = json.dumps(
            {
                "files": [
                    {"filename": "widget-5.0.0-py3-none-any.whl", "yanked": "oops"},
                    {"filename": "widget-5.0.0.tar.gz", "yanked": False},
                ]
            }
        ).encode()
        releases = by_version(parse_index(body, "widget", content_type="application/json"))
        assert releases["5.0.0"].yanked is False

    def test_one_compatible_file_makes_the_release_usable(self) -> None:
        body = json.dumps(
            {
                "files": [
                    {"filename": "widget-6.0.0-py3-none-any.whl", "requires-python": "<3.9"},
                    {"filename": "widget-6.0.0.tar.gz", "requires-python": ">=3.8"},
                ]
            }
        ).encode()
        releases = by_version(parse_index(body, "widget", content_type="application/json"))
        assert releases["6.0.0"].supports((3, 13, 0)) is True

    def test_release_with_no_compatible_file(self) -> None:
        body = json.dumps(
            {
                "files": [
                    {"filename": "widget-7.0.0-py3-none-any.whl", "requires-python": "<3.9"},
                    {"filename": "widget-7.0.0.tar.gz", "requires-python": "<3.9"},
                ]
            }
        ).encode()
        releases = by_version(parse_index(body, "widget", content_type="application/json"))
        assert releases["7.0.0"].supports((3, 13, 0)) is False

    def test_wheel_with_a_build_tag_still_yields_its_version(self) -> None:
        body = json.dumps({"files": [{"filename": "widget-8.0.0-1-py3-none-any.whl"}]}).encode()
        releases = by_version(parse_index(body, "widget", content_type="application/json"))
        assert set(releases) == {"8.0.0"}


class _Handler(BaseHTTPRequestHandler):
    body = JSON_BODY
    content_type = "application/vnd.pypi.simple.v1+json"
    status = 200
    seen_headers: ClassVar[dict[str, str]] = {}

    def do_GET(self) -> None:  # name fixed by http.server
        type(self).seen_headers = dict(self.headers)
        self.send_response(self.status)
        self.send_header("Content-Type", self.content_type)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def index_server():
    """A real HTTP index on localhost. Offline, but a genuine round trip."""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/simple/"
    server.shutdown()
    server.server_close()


class TestFetchOverHttp:
    def test_fetches_and_parses(self, index_server: str) -> None:
        releases = by_version(fetch_releases("widget", index_url=index_server))
        assert set(releases) == {"1.0.0", "1.1.0", "1.2.0rc1", "2.0.0"}

    def test_sends_the_json_accept_header_and_a_user_agent(self, index_server: str) -> None:
        fetch_releases("widget", index_url=index_server)
        headers = {k.lower(): v for k, v in _Handler.seen_headers.items()}
        assert "application/vnd.pypi.simple.v1+json" in headers["accept"]
        assert headers["user-agent"].startswith("depbisect/")

    def test_html_response(self, index_server: str, monkeypatch) -> None:
        monkeypatch.setattr(_Handler, "body", HTML_BODY)
        monkeypatch.setattr(_Handler, "content_type", "text/html")
        releases = by_version(fetch_releases("widget", index_url=index_server))
        assert set(releases) == {"1.0.0", "1.1.0", "2.0.0"}

    def test_404_names_the_project(self, index_server: str, monkeypatch) -> None:
        monkeypatch.setattr(_Handler, "status", 404)
        with pytest.raises(IndexError_, match="no such project"):
            fetch_releases("widget", index_url=index_server)

    def test_server_error_is_reported_with_its_status(self, index_server: str, monkeypatch) -> None:
        monkeypatch.setattr(_Handler, "status", 503)
        with pytest.raises(IndexError_, match="HTTP 503"):
            fetch_releases("widget", index_url=index_server)

    def test_unreachable_index(self) -> None:
        # Port 1 on loopback: refused immediately, no network needed.
        with pytest.raises(IndexError_, match="could not reach"):
            fetch_releases("widget", index_url="http://127.0.0.1:1/simple/", timeout=5)
