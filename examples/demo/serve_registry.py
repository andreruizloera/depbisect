"""Serve a small npm registry on localhost for part 5 of the demo.

Querying registry.npmjs.org would make the demo (and CI) depend on the
network and on a third party's release history, which is the same reason
serve_index.py exists for the Python parts. This serves one package,
padstr, as real package documents and real tarballs, so depbisect's
``--online`` path for a Node project runs its real HTTP request and its
real parsing, and every trial is a real ``npm install`` from the result.

padstr's releases are chosen so that each registry fact the candidate
filter reads appears exactly once:

- 1.0.0, 1.1.0 and 1.2.0 pad their input. 1.2.0 is deprecated and must
  stay a candidate, because npm installs a deprecated release asked for
  by its exact version.
- 1.3.0 stops padding. It is the regression the demo finds.
- 1.4.0-beta.1 is a pre-release, left out without ``--pre``.
- 1.5.0 is published for AIX only (``"os": ["aix"]``). npm refuses to
  install it on any other platform, so it is left out before a test run
  is spent on it.
- 2.0.0 is the bumped pin, and does not pad either.

Usage: python examples/demo/serve_registry.py PORTFILE
The chosen port is written to PORTFILE once the socket is listening.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import sys
import tarfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PACKAGE = "padstr"

PADS = "module.exports = (text, width) => String(text).padStart(width);\n"
DOES_NOT_PAD = "module.exports = (text, width) => String(text);\n"

# version -> (index.js, fields the registry lists for that version)
RELEASES: dict[str, tuple[str, dict[str, object]]] = {
    "1.0.0": (PADS, {}),
    "1.1.0": (PADS, {}),
    "1.2.0": (PADS, {"deprecated": "padstr 1.x is no longer maintained"}),
    "1.3.0": (DOES_NOT_PAD, {}),
    "1.4.0-beta.1": (DOES_NOT_PAD, {}),
    "1.5.0": (DOES_NOT_PAD, {"os": ["aix"]}),
    "2.0.0": (DOES_NOT_PAD, {}),
}


def build_tarball(version: str, index_js: str, os_list: object) -> bytes:
    """An npm package tarball: everything under package/, gzipped, reproducible."""
    manifest: dict[str, object] = {"name": PACKAGE, "version": version, "main": "index.js"}
    if os_list is not None:
        manifest["os"] = os_list
    files = {"package/package.json": json.dumps(manifest), "package/index.js": index_js}
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for arcname, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue(), mtime=0)


def build_document(base_url: str, tarballs: dict[str, bytes]) -> bytes:
    """The package document, with tarball URLs pointing back at this server."""
    versions: dict[str, object] = {}
    for version, (_, fields) in RELEASES.items():
        data = tarballs[version]
        versions[version] = {
            "name": PACKAGE,
            "version": version,
            **fields,
            "dist": {
                "tarball": f"{base_url}{PACKAGE}/-/{PACKAGE}-{version}.tgz",
                "shasum": hashlib.sha1(data).hexdigest(),
                "integrity": "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode(),
            },
        }
    document = {"name": PACKAGE, "dist-tags": {"latest": "2.0.0"}, "versions": versions}
    return json.dumps(document).encode()


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    port_file = Path(sys.argv[1])
    tarballs = {
        version: build_tarball(version, index_js, fields.get("os"))
        for version, (index_js, fields) in RELEASES.items()
    }
    tarball_prefix = f"/{PACKAGE}/-/{PACKAGE}-"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # name fixed by http.server
            content_type = "application/octet-stream"
            if self.path == f"/{PACKAGE}":
                body = build_document(base_url, tarballs)
                content_type = "application/json"
            elif self.path.startswith(tarball_prefix) and self.path.endswith(".tgz"):
                body = tarballs.get(self.path[len(tarball_prefix) : -len(".tgz")], b"")
            else:
                body = b""
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass  # keep the demo output readable

    server = HTTPServer(("127.0.0.1", 0), Handler)
    base_url = f"http://127.0.0.1:{server.server_address[1]}/"
    port_file.write_text(str(server.server_address[1]))
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
