"""Serve the demo wheels as a real PEP 503 package index on localhost.

The demo's second part needs an index to query, and querying PyPI would
make the demo (and CI) depend on the network and on a third party's
release history. This serves the committed wheels under
examples/demo/wheels/ over HTTP in the standard simple-repository
layout, so depbisect's ``--online`` path runs its real HTTP request,
its real response parsing, and real installs from the result.

One extra release exists only here: brokenlib 1.5.0 is published as a
Windows-only CPython 3.8 wheel. Nothing in CI or on a developer laptop
can install it, and its index entry states no Requires-Python, so the
metadata filter has no reason to drop it. It is the case the
three-valued bisection exists for: a candidate that fails to INSTALL
must be skipped and named, not blamed for the regression.

Usage: python examples/demo/serve_index.py PORTFILE
The chosen port is written to PORTFILE once the socket is listening.
"""

from __future__ import annotations

import html
import shutil
import sys
import tempfile
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from build_wheels import build_wheel

WHEELS_DIR = Path(__file__).parent / "wheels"

# name -> extra (version, wheel tag) releases published only at this index.
EXTRA = {"brokenlib": [("1.5.0", "cp38-cp38-win_amd64")]}


def build_tree(root: Path) -> None:
    packages = root / "packages"
    packages.mkdir(parents=True)
    by_project: dict[str, list[str]] = {}
    for wheel in sorted(WHEELS_DIR.glob("*.whl")):
        shutil.copy(wheel, packages / wheel.name)
        by_project.setdefault(wheel.name.split("-")[0], []).append(wheel.name)

    for project, releases in EXTRA.items():
        for version, tag in releases:
            built = build_wheel(packages, project, version, _PLACEHOLDER)
            renamed = packages / f"{project}-{version}-{tag}.whl"
            built.rename(renamed)
            by_project.setdefault(project, []).append(renamed.name)

    simple = root / "simple"
    simple.mkdir()
    for project, filenames in by_project.items():
        project_dir = simple / project
        project_dir.mkdir()
        anchors = "\n".join(
            f'    <a href="../../packages/{html.escape(name)}">{html.escape(name)}</a><br/>'
            for name in sorted(filenames)
        )
        (project_dir / "index.html").write_text(
            f"<!DOCTYPE html>\n<html><body>\n{anchors}\n</body></html>\n"
        )


_PLACEHOLDER = '''\
"""brokenlib {version}: a release that cannot be installed here."""

__version__ = "{version}"


def greet(name: str) -> str:
    return f"hello, {{name}}"
'''


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    port_file = Path(sys.argv[1])
    root = Path(tempfile.mkdtemp(prefix="depbisect-index-"))
    try:
        build_tree(root)

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args: object, **kwargs: object) -> None:
                super().__init__(*args, directory=str(root), **kwargs)  # type: ignore[arg-type]

            def log_message(self, *args: object) -> None:
                pass  # keep the demo output readable

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port_file.write_text(str(server.server_address[1]))
        server.serve_forever()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
