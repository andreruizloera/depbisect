"""Manifest and lockfile parsing for Python and Node projects.

Everything here works on file *content* (strings), not paths, so the
same parsers serve both the working tree and ``git show`` blobs. Each
parser produces a DepState: a flat map of package name to pinned
version, plus a list of requirements we could not pin down.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from depbisect.errors import DepbisectError
from depbisect.versions import normalize_name

# Files we know how to read, in priority order per ecosystem. Lockfiles
# beat manifests because they carry exact versions.
PYTHON_SOURCES = (
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "requirements.txt",
    "pyproject.toml",
)
NODE_SOURCES = ("package-lock.json",)


@dataclass
class DepState:
    """Pinned dependency versions extracted from one manifest/lockfile."""

    ecosystem: str  # "python" or "node"
    source: str  # relative path of the file the pins came from
    pins: dict[str, str] = field(default_factory=dict)
    unpinned: list[str] = field(default_factory=list)


def detect_ecosystem(project: Path) -> str:
    """Detect whether a project is Python or Node, or fail cleanly."""
    if any((project / name).is_file() for name in PYTHON_SOURCES):
        return "python"
    if (project / "package.json").is_file():
        return "node"
    raise DepbisectError(
        f"no supported manifest found in {project} (looked for "
        + ", ".join(PYTHON_SOURCES)
        + ", package.json)"
    )


def pick_source(project: Path, ecosystem: str) -> str:
    """Choose the file to diff, preferring lockfiles over manifests."""
    names = PYTHON_SOURCES if ecosystem == "python" else NODE_SOURCES
    for name in names:
        if (project / name).is_file():
            return name
    if ecosystem == "node":
        raise DepbisectError(
            "node support needs package-lock.json next to package.json; run npm install first"
        )
    raise DepbisectError(f"no python manifest found in {project}")


def parse_state(source: str, content: str, ecosystem: str) -> DepState:
    """Parse manifest content into a DepState based on its filename."""
    if ecosystem == "python":
        if source.endswith("uv.lock"):
            return _parse_uv_lock(source, content)
        if source.endswith("poetry.lock"):
            return _parse_poetry_lock(source, content)
        if source.endswith("Pipfile.lock"):
            return _parse_pipfile_lock(source, content)
        if source.endswith("pyproject.toml"):
            return _parse_pyproject(source, content)
        return _parse_requirements(source, content)
    if source.endswith("package-lock.json"):
        return _parse_package_lock(source, content)
    raise DepbisectError(f"do not know how to parse {source}")


# --- Python -----------------------------------------------------------

_REQ_PIN_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*(?P<version>[^\s;#]+)"
)


def _parse_requirements(source: str, content: str) -> DepState:
    state = DepState("python", source)
    for raw in content.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = _REQ_PIN_RE.match(line)
        if m:
            state.pins[normalize_name(m.group("name"))] = m.group("version")
        else:
            state.unpinned.append(line)
    return state


def _parse_uv_lock(source: str, content: str) -> DepState:
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise DepbisectError(f"cannot parse {source}: {exc}") from exc
    state = DepState("python", source)
    for pkg in data.get("package", []):
        name, version = pkg.get("name"), pkg.get("version")
        if not name or not version:
            continue
        src = pkg.get("source", {})
        # Skip the project itself and other local path/editable packages;
        # their "versions" do not change through the package index.
        if any(key in src for key in ("editable", "virtual", "directory")):
            continue
        state.pins[normalize_name(name)] = str(version)
    return state


#: poetry.lock ``[package.source]`` types whose "version" is not
#: something a package index can hand you a different release of.
_POETRY_LOCAL_SOURCES = frozenset({"directory", "file", "url", "git"})
#: The Pipfile.lock keys that mean the same thing.
_PIPENV_LOCAL_KEYS = frozenset({"path", "file", "url", "git"})


def _parse_poetry_lock(source: str, content: str) -> DepState:
    """Parse a poetry.lock (lock-version 1.x and 2.x share this shape).

    Every locked package is an ``[[package]]`` table with a name and a
    version, whichever dependency group it came from: a bisection wants
    the test dependencies too. Packages resolved from a directory, a
    file, a URL, or git are skipped, exactly as uv.lock's local packages
    are, because there is no release series to bisect for them.
    """
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise DepbisectError(f"cannot parse {source}: {exc}") from exc
    state = DepState("python", source)
    packages = data.get("package")
    if not isinstance(packages, list):
        return state
    for pkg in packages:
        if not isinstance(pkg, dict):
            continue
        name, version = pkg.get("name"), pkg.get("version")
        if not name or not version:
            continue
        src = pkg.get("source")
        if isinstance(src, dict) and src.get("type") in _POETRY_LOCAL_SOURCES:
            continue
        state.pins[normalize_name(str(name))] = str(version)
    return state


def _parse_pipfile_lock(source: str, content: str) -> DepState:
    """Parse a Pipfile.lock: JSON, with "default" and "develop" sections.

    Pipenv writes versions as a specifier string (``"==2.31.0"``), so
    the operator comes off. An entry pinned to a path, a file, or a git
    ref has no index version to walk and is recorded as unpinned, which
    is how the report already talks about a dependency it cannot bisect.
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise DepbisectError(f"cannot parse {source}: {exc}") from exc
    state = DepState("python", source)
    for section in ("default", "develop"):
        entries = data.get(section)
        if not isinstance(entries, dict):
            continue
        for name, info in entries.items():
            if not isinstance(info, dict):
                continue
            if info.get("editable") or _PIPENV_LOCAL_KEYS & set(info):
                state.unpinned.append(str(name))
                continue
            version = info.get("version")
            if not isinstance(version, str):
                state.unpinned.append(str(name))
                continue
            pinned = version.strip()
            if not pinned.startswith("=="):
                # A Pipfile.lock entry that is not an exact pin cannot
                # anchor a bisection; naming it beats guessing at it.
                state.unpinned.append(f"{name}{pinned}")
                continue
            state.pins[normalize_name(str(name))] = pinned[2:].strip()
    return state


def _parse_pyproject(source: str, content: str) -> DepState:
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise DepbisectError(f"cannot parse {source}: {exc}") from exc
    state = DepState("python", source)
    for req in data.get("project", {}).get("dependencies", []):
        m = _REQ_PIN_RE.match(req)
        if m:
            state.pins[normalize_name(m.group("name"))] = m.group("version")
        else:
            state.unpinned.append(req)
    return state


# --- Node -------------------------------------------------------------


def _parse_package_lock(source: str, content: str) -> DepState:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise DepbisectError(f"cannot parse {source}: {exc}") from exc
    state = DepState("node", source)
    # Track the shallowest occurrence of each package: when a package
    # appears nested at several versions, the top-level resolution wins.
    best: dict[str, tuple[int, str]] = {}
    packages = data.get("packages")
    if isinstance(packages, dict):  # lockfile v2/v3
        for key, info in packages.items():
            if not key or not isinstance(info, dict):
                continue  # "" is the root project
            name = key.rsplit("node_modules/", 1)[-1]
            version = info.get("version")
            if not version:
                continue
            depth = key.count("node_modules/")
            if name not in best or best[name][0] > depth:
                best[name] = (depth, str(version))
    else:  # lockfile v1

        def walk(deps: dict, depth: int) -> None:
            for name, info in deps.items():
                if not isinstance(info, dict):
                    continue
                version = info.get("version")
                if version and (name not in best or best[name][0] > depth):
                    best[name] = (depth, str(version))
                walk(info.get("dependencies", {}), depth + 1)

        walk(data.get("dependencies", {}), 0)
    state.pins = {name: version for name, (_, version) in sorted(best.items())}
    return state


# --- Trial manifest generation ---------------------------------------


def render_requirements(pins: dict[str, str]) -> str:
    """Render a pin set as a requirements file for a trial install."""
    lines = [f"{name}=={version}" for name, version in sorted(pins.items())]
    return "\n".join(lines) + "\n"


def render_package_json(original: str, pins: dict[str, str]) -> str:
    """Rewrite package.json dependency specs to exact trial versions."""
    data = json.loads(original)
    for section in ("dependencies", "devDependencies"):
        deps = data.get(section)
        if not isinstance(deps, dict):
            continue
        for name in deps:
            if name in pins:
                deps[name] = pins[name]
    return json.dumps(data, indent=2) + "\n"
