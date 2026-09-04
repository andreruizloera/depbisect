"""Manifest and lockfile parsing for both ecosystems."""

import json
from pathlib import Path

import pytest

from depbisect.errors import DepbisectError
from depbisect.manifests import (
    detect_ecosystem,
    parse_state,
    pick_source,
    render_package_json,
    render_requirements,
)


class TestRequirements:
    def test_pins_comments_and_options(self) -> None:
        content = """\
# comment line
httpx==0.28.0  # trailing comment
requests[socks]==2.32.0
-r other.txt
--index-url https://example.invalid/simple
numpy>=1.26
Django==5.0.1 ; python_version >= "3.10"
"""
        state = parse_state("requirements.txt", content, "python")
        assert state.pins == {
            "httpx": "0.28.0",
            "requests": "2.32.0",
            "django": "5.0.1",
        }
        assert state.unpinned == ["numpy>=1.26"]

    def test_names_normalized(self) -> None:
        state = parse_state("requirements.txt", "My_Pkg==1.0\n", "python")
        assert state.pins == {"my-pkg": "1.0"}


class TestUvLock:
    def test_packages_extracted_and_local_skipped(self) -> None:
        content = """\
version = 1

[[package]]
name = "myproject"
version = "0.1.0"
source = { editable = "." }

[[package]]
name = "httpx"
version = "0.28.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "certifi"
version = "2024.8.30"
source = { registry = "https://pypi.org/simple" }
"""
        state = parse_state("uv.lock", content, "python")
        assert state.pins == {"httpx": "0.28.0", "certifi": "2024.8.30"}

    def test_invalid_toml(self) -> None:
        with pytest.raises(DepbisectError, match="cannot parse"):
            parse_state("uv.lock", "not [ valid toml", "python")


class TestPyproject:
    def test_pinned_dependencies(self) -> None:
        content = """\
[project]
name = "app"
version = "1.0"
dependencies = ["httpx==0.28.0", "numpy>=1.26"]
"""
        state = parse_state("pyproject.toml", content, "python")
        assert state.pins == {"httpx": "0.28.0"}
        assert state.unpinned == ["numpy>=1.26"]


class TestPackageLock:
    def test_v3_top_level_wins_over_nested(self) -> None:
        content = json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "app", "version": "1.0.0"},
                    "node_modules/left-pad": {"version": "1.3.0"},
                    "node_modules/express": {"version": "4.19.0"},
                    "node_modules/express/node_modules/left-pad": {"version": "1.1.0"},
                },
            }
        )
        state = parse_state("package-lock.json", content, "node")
        assert state.pins == {"express": "4.19.0", "left-pad": "1.3.0"}

    def test_v1_dependencies_tree(self) -> None:
        content = json.dumps(
            {
                "lockfileVersion": 1,
                "dependencies": {
                    "express": {
                        "version": "4.19.0",
                        "dependencies": {"left-pad": {"version": "1.1.0"}},
                    },
                    "left-pad": {"version": "1.3.0"},
                },
            }
        )
        state = parse_state("package-lock.json", content, "node")
        assert state.pins == {"express": "4.19.0", "left-pad": "1.3.0"}

    def test_scoped_packages(self) -> None:
        content = json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "app"},
                    "node_modules/@types/node": {"version": "20.11.0"},
                },
            }
        )
        state = parse_state("package-lock.json", content, "node")
        assert state.pins == {"@types/node": "20.11.0"}

    def test_invalid_json(self) -> None:
        with pytest.raises(DepbisectError, match="cannot parse"):
            parse_state("package-lock.json", "{oops", "node")


class TestDetection:
    def test_python_beats_node_when_both(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("a==1\n")
        (tmp_path / "package.json").write_text("{}")
        assert detect_ecosystem(tmp_path) == "python"

    def test_node(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}")
        assert detect_ecosystem(tmp_path) == "node"

    def test_nothing_found(self, tmp_path: Path) -> None:
        with pytest.raises(DepbisectError, match="no supported manifest"):
            detect_ecosystem(tmp_path)

    def test_lockfile_preferred(self, tmp_path: Path) -> None:
        (tmp_path / "uv.lock").write_text("version = 1\n")
        (tmp_path / "requirements.txt").write_text("a==1\n")
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        assert pick_source(tmp_path, "python") == "uv.lock"

    def test_node_requires_lockfile(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}")
        with pytest.raises(DepbisectError, match=r"package-lock\.json"):
            pick_source(tmp_path, "node")


class TestRendering:
    def test_requirements_sorted(self) -> None:
        out = render_requirements({"b": "2.0", "a": "1.0"})
        assert out == "a==1.0\nb==2.0\n"

    def test_package_json_overrides_only_known(self) -> None:
        original = json.dumps(
            {
                "name": "app",
                "dependencies": {"express": "^4.18.0", "left-pad": "~1.2.0"},
                "devDependencies": {"jest": "^29.0.0"},
            }
        )
        out = render_package_json(original, {"express": "4.19.0", "jest": "29.7.0"})
        data = json.loads(out)
        assert data["dependencies"] == {"express": "4.19.0", "left-pad": "~1.2.0"}
        assert data["devDependencies"] == {"jest": "29.7.0"}
