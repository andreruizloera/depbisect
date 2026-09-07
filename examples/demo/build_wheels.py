"""Regenerate the demo's local wheels, fully offline.

The wheels under examples/demo/wheels/ are committed so the demo works
without network access; this script exists so they can be rebuilt or
audited. Run: python examples/demo/build_wheels.py
"""

from __future__ import annotations

import base64
import hashlib
import io
import tarfile
import zipfile
from pathlib import Path

WHEELS_DIR = Path(__file__).parent / "wheels"

GOOD_GREET = '''\
"""brokenlib {version}: string helpers for the demo app."""

__version__ = "{version}"


def greet(name: str) -> str:
    return f"hello, {{name}}"
'''

# Version 2.0.0 changes greet's output format: exactly the kind of
# quiet behavior change that breaks a downstream test suite.
BAD_GREET = '''\
"""brokenlib {version}: string helpers for the demo app."""

__version__ = "{version}"


def greet(name: str) -> str:
    return f"Greetings, esteemed {{name}}!"
'''

OKPKG_10 = '''\
"""okpkg {version}: arithmetic helpers for the demo app."""

__version__ = "{version}"


def add(a: int, b: int) -> int:
    return a + b
'''

OKPKG_11 = (
    OKPKG_10
    + """

def mul(a: int, b: int) -> int:
    return a * b
"""
)

RELEASES: list[tuple[str, str, str]] = [
    ("brokenlib", "1.0.0", GOOD_GREET),
    ("brokenlib", "1.1.0", GOOD_GREET),
    ("brokenlib", "1.2.0", GOOD_GREET),
    ("brokenlib", "2.0.0", BAD_GREET),
    ("okpkg", "1.0.0", OKPKG_10),
    ("okpkg", "1.1.0", OKPKG_11),
]


def build_wheel(directory: Path, name: str, version: str, init_source: str) -> Path:
    wheel_path = directory / f"{name}-{version}-py3-none-any.whl"
    dist_info = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": init_source.format(version=version),
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            f"Summary: depbisect demo fixture package\n"
        ),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: depbisect-demo\nRoot-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ),
    }
    record_lines = []
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, content in files.items():
            data = content.encode()
            zf.writestr(zipfile.ZipInfo(arcname, (2020, 1, 1, 0, 0, 0)), data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
            record_lines.append(f"{arcname},sha256={digest.decode()},{len(data)}")
        record_lines.append(f"{dist_info}/RECORD,,")
        zf.writestr(
            zipfile.ZipInfo(f"{dist_info}/RECORD", (2020, 1, 1, 0, 0, 0)),
            "\n".join(record_lines) + "\n",
        )
    return wheel_path


# A source distribution whose build fails, on purpose and offline. Real
# sdists fail to build for ordinary reasons: a missing compiler, a header
# that is not installed, a build requirement that cannot be resolved.
# Reproducing any of those here would need a toolchain or a network, so
# the fixture uses an in-tree PEP 517 backend that raises. What matters
# for the demo is the shape of the outcome, not the cause: depbisect must
# NOT exclude an sdist-only release on wheel tags, because an sdist can
# build; it finds out by trying, and reports a skip when it fails.
_FAILING_BACKEND = '''\
"""In-tree PEP 517 backend for the demo fixture. Every hook fails."""


def _refuse(*args, **kwargs):
    raise RuntimeError(
        "brokenlib {version} has no prebuilt wheel and its build fails here "
        "(depbisect demo fixture)"
    )


get_requires_for_build_wheel = _refuse
prepare_metadata_for_build_wheel = _refuse
build_wheel = _refuse
build_sdist = _refuse
'''

_FAILING_PYPROJECT = """\
[build-system]
requires = []
build-backend = "demo_backend"
backend-path = ["."]

[project]
name = "{name}"
version = "{version}"
"""


def build_failing_sdist(directory: Path, name: str, version: str) -> Path:
    """Write ``name-version.tar.gz``, a source-only release that will not build."""
    root = f"{name}-{version}"
    files = {
        "pyproject.toml": _FAILING_PYPROJECT.format(name=name, version=version),
        "demo_backend.py": _FAILING_BACKEND.format(version=version),
        "PKG-INFO": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            f"Summary: depbisect demo fixture, source only\n"
        ),
    }
    sdist_path = directory / f"{root}.tar.gz"
    with tarfile.open(sdist_path, "w:gz") as tar:
        for relative, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(f"{root}/{relative}")
            info.size = len(data)
            info.mtime = 1577836800  # 2020-01-01, so the archive is reproducible
            tar.addfile(info, io.BytesIO(data))
    return sdist_path


def main() -> None:
    WHEELS_DIR.mkdir(exist_ok=True)
    for name, version, source in RELEASES:
        path = build_wheel(WHEELS_DIR, name, version, source)
        print(f"built {path.name}")


if __name__ == "__main__":
    main()
