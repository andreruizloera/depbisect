"""Serve the demo wheels as a real PEP 503 package index on localhost.

The demo's second part needs an index to query, and querying PyPI would
make the demo (and CI) depend on the network and on a third party's
release history. This serves the committed wheels under
examples/demo/wheels/ over HTTP in the standard simple-repository
layout, so depbisect's ``--online`` path runs its real HTTP request,
its real response parsing, and real installs from the result.

Two extra releases exist only here, and they are deliberately different
from each other:

- brokenlib 1.5.0 is published as a Windows-only CPython 3.8 wheel and
  nothing else. Its wheel tag says, in the index listing, that it cannot
  be installed on this host, so depbisect drops it from the candidate
  path without spending a test run on it.
- brokenlib 1.6.0 is published as a source distribution and nothing
  else. An sdist has no tags and may well build here, so depbisect must
  NOT drop it: it stays a candidate, gets probed, and fails to build.
  That is the case the three-valued bisection exists for, and the
  release is skipped and named rather than blamed for the regression.

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

from build_wheels import build_failing_sdist, build_wheel

WHEELS_DIR = Path(__file__).parent / "wheels"

# name -> extra (version, wheel tag) releases published only at this index.
EXTRA_WHEELS = {"brokenlib": [("1.5.0", "cp38-cp38-win_amd64")]}
# name -> versions published here as a source distribution and nothing else.
EXTRA_SDISTS = {"brokenlib": ["1.6.0"]}


def build_tree(root: Path) -> None:
    packages = root / "packages"
    packages.mkdir(parents=True)
    by_project: dict[str, list[str]] = {}
    for wheel in sorted(WHEELS_DIR.glob("*.whl")):
        shutil.copy(wheel, packages / wheel.name)
        by_project.setdefault(wheel.name.split("-")[0], []).append(wheel.name)

    for project, releases in EXTRA_WHEELS.items():
        for version, tag in releases:
            built = build_wheel(packages, project, version, _PLACEHOLDER)
            renamed = packages / f"{project}-{version}-{tag}.whl"
            built.rename(renamed)
            by_project.setdefault(project, []).append(renamed.name)

    for project, versions in EXTRA_SDISTS.items():
        for version in versions:
            sdist = build_failing_sdist(packages, project, version)
            by_project.setdefault(project, []).append(sdist.name)

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
