"""The safety layer: every install and test run happens in a throwaway copy.

Guarantee: depbisect never modifies the user's project. The project
tree is copied into a temporary directory (skipping .git, virtualenvs,
node_modules, and caches), trial manifests are written only inside that
copy, installs go into a fresh virtualenv inside the copy, and the test
command runs with the copy as its working directory. When the run ends
the copy is deleted unless --keep-temp is passed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from depbisect.errors import DepbisectError
from depbisect.manifests import render_package_json, render_requirements

_SKIP_DIRS = {
    ".git",
    ".hg",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".depbisect",
}

TRIAL_REQUIREMENTS = "requirements.depbisect.txt"


class Workspace:
    """A disposable copy of the project where trials actually run."""

    def __init__(
        self,
        project: Path,
        ecosystem: str,
        *,
        keep: bool = False,
        no_index: bool = False,
        find_links: list[Path] | None = None,
        timeout: int = 600,
        verbose: bool = False,
    ) -> None:
        self.project = project.resolve()
        self.ecosystem = ecosystem
        self.keep = keep
        self.no_index = no_index
        self.find_links = [p.resolve() for p in (find_links or [])]
        self.timeout = timeout
        self.verbose = verbose
        self.root: Path | None = None
        self._original_package_json: str | None = None

    # -- lifecycle -----------------------------------------------------

    def __enter__(self) -> Workspace:
        self.root = Path(tempfile.mkdtemp(prefix="depbisect-"))
        self.copy_dir = self.root / "project"
        shutil.copytree(
            self.project,
            self.copy_dir,
            ignore=shutil.ignore_patterns(*_SKIP_DIRS),
            symlinks=True,
        )
        pkg = self.copy_dir / "package.json"
        if self.ecosystem == "node" and pkg.is_file():
            self._original_package_json = pkg.read_text()
        return self

    def __exit__(self, *exc: object) -> None:
        if self.root and not self.keep:
            shutil.rmtree(self.root, ignore_errors=True)

    # -- one trial -----------------------------------------------------

    def run_trial(self, pins: dict[str, str], test_cmd: str) -> bool:
        """Install ``pins`` in a fresh env inside the copy, run the test.

        Returns True when the test command exits 0. A combination that
        fails to install counts as a failing trial (with a warning)
        rather than aborting the whole search, since conflicting pins
        are a normal thing to hit mid-bisect.
        """
        assert self.root is not None, "Workspace must be entered first"
        self._apply(pins)
        try:
            env_dir = self._install(pins)
        except DepbisectError as exc:
            print(f"warning: trial install failed, counting as FAIL: {exc}", file=sys.stderr)
            return False
        return self._run_test(test_cmd, env_dir)

    # -- internals -----------------------------------------------------

    def _apply(self, pins: dict[str, str]) -> None:
        if self.ecosystem == "python":
            (self.copy_dir / TRIAL_REQUIREMENTS).write_text(render_requirements(pins))
        else:
            assert self._original_package_json is not None
            (self.copy_dir / "package.json").write_text(
                render_package_json(self._original_package_json, pins)
            )
            lock = self.copy_dir / "package-lock.json"
            if lock.exists():
                lock.unlink()  # inside the copy only; npm regenerates it

    def _install(self, pins: dict[str, str]) -> Path | None:
        if self.ecosystem == "node":
            self._run(
                ["npm", "install", "--no-audit", "--no-fund", "--loglevel=error"],
                cwd=self.copy_dir,
                what="npm install",
            )
            return None

        env_dir = Path(tempfile.mkdtemp(prefix="venv-", dir=self.root))
        shutil.rmtree(env_dir)  # uv/venv want to create it themselves
        uv = shutil.which("uv")
        req = self.copy_dir / TRIAL_REQUIREMENTS
        index_args: list[str] = []
        if self.no_index:
            index_args.append("--no-index")
        for links in self.find_links:
            index_args += ["--find-links", str(links)]
        if uv:
            self._run([uv, "venv", "--quiet", str(env_dir)], cwd=self.copy_dir, what="uv venv")
            self._run(
                [
                    uv,
                    "pip",
                    "install",
                    "--quiet",
                    "--python",
                    str(env_dir / "bin" / "python"),
                    "-r",
                    str(req),
                    *index_args,
                ],
                cwd=self.copy_dir,
                what="uv pip install",
            )
        else:
            self._run([sys.executable, "-m", "venv", str(env_dir)], cwd=self.copy_dir, what="venv")
            self._run(
                [
                    str(env_dir / "bin" / "python"),
                    "-m",
                    "pip",
                    "install",
                    "--quiet",
                    "-r",
                    str(req),
                    *index_args,
                ],
                cwd=self.copy_dir,
                what="pip install",
            )
        return env_dir

    def _run_test(self, test_cmd: str, env_dir: Path | None) -> bool:
        env = os.environ.copy()
        if env_dir is not None:
            env["VIRTUAL_ENV"] = str(env_dir)
            env["PATH"] = f"{env_dir / 'bin'}{os.pathsep}{env['PATH']}"
            env.pop("PYTHONHOME", None)
        try:
            result = subprocess.run(
                test_cmd,
                shell=True,
                cwd=self.copy_dir,
                env=env,
                capture_output=not self.verbose,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            return False
        return result.returncode == 0

    def _run(self, cmd: list[str], *, cwd: Path, what: str) -> None:
        try:
            result = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True, timeout=self.timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise DepbisectError(f"{what} timed out after {self.timeout}s") from exc
        except FileNotFoundError as exc:
            raise DepbisectError(f"{what} failed: {cmd[0]} not found on PATH") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise DepbisectError(f"{what} failed:\n{detail}")
