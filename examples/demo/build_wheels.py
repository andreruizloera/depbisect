"""Regenerate the demo's local wheels, fully offline.

The wheels under examples/demo/wheels/ are committed so the demo works
without network access; this script exists so they can be rebuilt or
audited. Run: python examples/demo/build_wheels.py
"""

from __future__ import annotations

import base64
import hashlib
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


def main() -> None:
    WHEELS_DIR.mkdir(exist_ok=True)
    for name, version, source in RELEASES:
        path = build_wheel(WHEELS_DIR, name, version, source)
        print(f"built {path.name}")


if __name__ == "__main__":
    main()
