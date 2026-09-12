"""npm package tarballs in a local directory, read as Node candidates.

``npm pack`` names a tarball ``<name>-<version>.tgz``, dropping a scoped
name's ``@`` and turning its ``/`` into a dash, so ``@scope/pkg`` 1.1.0
is ``scope-pkg-1.1.0.tgz``. A package name and a semver version may both
contain dashes, so the filename alone cannot be split: ``my-pkg-1.0.0.tgz``
starts with ``my-`` too. The filename only decides which tarballs are
opened; the name and version come from the ``package.json`` inside,
which is what npm itself installs from.

This module reads files and nothing else. It never talks to a registry.
"""

from __future__ import annotations

import json
import tarfile
import zlib
from pathlib import Path

TARBALL_SUFFIX = ".tgz"


def pack_prefix(package: str) -> str:
    """The filename prefix ``npm pack`` gives every tarball of ``package``."""
    return package.removeprefix("@").replace("/", "-") + "-"


def read_manifest(path: Path) -> tuple[str, str] | None:
    """``(name, version)`` from the top-level ``package.json`` of a tarball.

    npm packs everything under one directory (``package/``), so the
    manifest is the ``package.json`` exactly one level down; a deeper one
    belongs to a bundled dependency and is not this package's. Returns
    None for anything that is not a readable package tarball.
    """
    try:
        with tarfile.open(path, mode="r:gz") as tar:
            for member in tar:
                parts = member.name.removeprefix("./").split("/")
                if len(parts) == 2 and parts[1] == "package.json" and member.isfile():
                    handle = tar.extractfile(member)
                    if handle is None:
                        return None
                    data = json.loads(handle.read())
                    break
            else:
                return None
    except (OSError, EOFError, zlib.error, tarfile.TarError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    name, version = data.get("name"), data.get("version")
    if not isinstance(name, str) or not isinstance(version, str) or not name or not version:
        return None
    return name, version


def local_tarballs(package: str, find_links: list[Path]) -> dict[str, Path]:
    """Each version of ``package`` packed in ``find_links``, with its tarball.

    When two tarballs hold the same version, the one in the directory
    given first wins, and within a directory the first by filename.
    """
    prefix = pack_prefix(package)
    found: dict[str, Path] = {}
    for directory in find_links:
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if not (entry.name.startswith(prefix) and entry.name.endswith(TARBALL_SUFFIX)):
                continue
            if not entry.is_file():
                continue
            manifest = read_manifest(entry)
            if manifest is None or manifest[0] != package:
                continue
            found.setdefault(manifest[1], entry.resolve())
    return found
