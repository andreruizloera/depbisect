"""Fixtures shared across test files: npm package tarballs written to disk."""

from __future__ import annotations

import gzip
import io
import json
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest


def _tarball_bytes(files: dict[str, str]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for arcname, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue(), mtime=0)


@pytest.fixture
def write_tarball() -> Callable[[Path, dict[str, str]], Path]:
    """``write_tarball(path, {arcname: text})`` writes a gzipped tar of exactly those files."""

    def write(path: Path, files: dict[str, str]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_tarball_bytes(files))
        return path

    return write


@pytest.fixture
def write_package(write_tarball) -> Callable[..., Path]:
    """``write_package(path, name, version, index_js=...)``: a tarball shaped like npm pack's."""

    def write(path: Path, name: str, version: str, index_js: str = "module.exports = 1;\n") -> Path:
        manifest = json.dumps({"name": name, "version": version, "main": "index.js"})
        return write_tarball(path, {"package/package.json": manifest, "package/index.js": index_js})

    return write
