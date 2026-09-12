"""Reading npm pack tarballs out of a --find-links directory."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from depbisect.tarballs import local_tarballs, pack_prefix, read_manifest


@pytest.mark.parametrize(
    ("package", "prefix"),
    [
        ("padstr", "padstr-"),
        ("lodash.merge", "lodash.merge-"),
        ("@demo/pad-str", "demo-pad-str-"),
    ],
)
def test_pack_prefix(package: str, prefix: str) -> None:
    assert pack_prefix(package) == prefix


class TestLocalTarballs:
    def test_each_version_comes_with_its_tarball(self, tmp_path: Path, write_package) -> None:
        packs = tmp_path / "packs"
        one = write_package(packs / "padstr-1.1.0.tgz", "padstr", "1.1.0")
        two = write_package(packs / "padstr-1.2.0.tgz", "padstr", "1.2.0")
        assert local_tarballs("padstr", [packs]) == {"1.1.0": one.resolve(), "1.2.0": two.resolve()}

    def test_what_npm_pack_really_writes_for_a_scoped_prerelease(self, tmp_path: Path) -> None:
        # The filename rule is npm's, so it is checked against npm itself.
        if shutil.which("npm") is None:
            pytest.skip("needs npm on PATH")
        source = tmp_path / "source"
        source.mkdir()
        (source / "package.json").write_text(
            '{"name": "@demo/pad-str", "version": "1.0.0-beta.1"}\n'
        )
        (source / "index.js").write_text("module.exports = 1;\n")
        packs = tmp_path / "packs"
        packs.mkdir()
        env = dict(os.environ, npm_config_cache=str(tmp_path / "npm-cache"))
        subprocess.run(
            ["npm", "pack", "--pack-destination", str(packs)],
            cwd=source,
            env=env,
            check=True,
            capture_output=True,
            timeout=120,
        )
        assert [entry.name for entry in packs.iterdir()] == ["demo-pad-str-1.0.0-beta.1.tgz"]
        assert list(local_tarballs("@demo/pad-str", [packs])) == ["1.0.0-beta.1"]

    def test_the_version_is_the_manifests_not_the_filenames(
        self, tmp_path: Path, write_package
    ) -> None:
        # npm installs what the package.json inside says, so that is the answer.
        packs = tmp_path / "packs"
        write_package(packs / "padstr-9.9.9.tgz", "padstr", "1.1.0")
        assert list(local_tarballs("padstr", [packs])) == ["1.1.0"]

    def test_a_longer_name_sharing_the_prefix_is_another_package(
        self, tmp_path: Path, write_package
    ) -> None:
        packs = tmp_path / "packs"
        write_package(packs / "my-pkg-1.0.0.tgz", "my-pkg", "1.0.0")
        assert local_tarballs("my", [packs]) == {}
        assert list(local_tarballs("my-pkg", [packs])) == ["1.0.0"]

    def test_unreadable_tarballs_are_skipped(
        self, tmp_path: Path, write_tarball, write_package
    ) -> None:
        packs = tmp_path / "packs"
        packs.mkdir()
        (packs / "padstr-1.0.0.tgz").write_bytes(b"not a tarball")
        good = write_package(tmp_path / "whole.tgz", "padstr", "1.0.1")
        (packs / "padstr-1.0.1.tgz").write_bytes(good.read_bytes()[:40])  # truncated gzip
        write_tarball(packs / "padstr-1.1.0.tgz", {"package/index.js": "module.exports = 1;\n"})
        write_tarball(packs / "padstr-1.2.0.tgz", {"package/package.json": "{not json"})
        # A deeper package.json belongs to a bundled dependency, not to this package.
        write_tarball(
            packs / "padstr-1.3.0.tgz",
            {"package/node_modules/padstr/package.json": '{"name": "padstr", "version": "1.3.0"}'},
        )
        write_package(packs / "padstr-1.4.0.tgz", "padstr", "1.4.0")
        assert list(local_tarballs("padstr", [packs])) == ["1.4.0"]

    def test_the_first_directory_wins_a_duplicate_version(
        self, tmp_path: Path, write_package
    ) -> None:
        first = write_package(tmp_path / "a" / "padstr-1.1.0.tgz", "padstr", "1.1.0")
        write_package(tmp_path / "b" / "padstr-1.1.0.tgz", "padstr", "1.1.0")
        assert local_tarballs("padstr", [tmp_path / "a", tmp_path / "b"]) == {
            "1.1.0": first.resolve()
        }

    def test_python_distributions_and_missing_directories_supply_nothing(
        self, tmp_path: Path
    ) -> None:
        packs = tmp_path / "packs"
        packs.mkdir()
        (packs / "padstr-1.1.0-py3-none-any.whl").write_bytes(b"")
        (packs / "padstr-1.2.0.tar.gz").write_bytes(b"")
        assert local_tarballs("padstr", [packs, tmp_path / "missing"]) == {}


def test_read_manifest_accepts_a_leading_dot_slash(tmp_path: Path, write_tarball) -> None:
    path = write_tarball(
        tmp_path / "padstr-1.0.0.tgz",
        {"./package/package.json": '{"name": "padstr", "version": "1.0.0"}'},
    )
    assert read_manifest(path) == ("padstr", "1.0.0")
