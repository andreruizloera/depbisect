"""The safety layer: the user's project must come out byte-identical."""

import hashlib
import subprocess
from pathlib import Path

from depbisect.sandbox import TRIAL_REQUIREMENTS, Workspace


def tree_digest(root: Path) -> dict[str, str]:
    """Relative path -> sha256 for every file under root."""
    digest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


def make_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "requirements.txt").write_text("brokenlib==2.0.0\n")
    (project / "app.py").write_text("print('hello')\n")
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("[core]\n")
    (project / "node_modules").mkdir()
    (project / "node_modules" / "junk.js").write_text("x")
    return project


class TestWorkspaceSafety:
    def test_original_untouched_after_trials(self, tmp_path: Path, monkeypatch) -> None:
        project = make_project(tmp_path)
        before = tree_digest(project)

        # Stub out install and test so no real subprocesses run.
        monkeypatch.setattr(Workspace, "_install", lambda self, pins: None)
        monkeypatch.setattr(Workspace, "_run_test", lambda self, cmd, env: True)

        with Workspace(project, "python") as ws:
            assert ws.run_trial({"brokenlib": "1.0.0"}, "pytest") is True
            assert ws.run_trial({"brokenlib": "1.5.0"}, "pytest") is True
            # The trial manifest lands in the copy, never the original.
            assert (ws.copy_dir / TRIAL_REQUIREMENTS).is_file()

        assert tree_digest(project) == before
        assert not (project / TRIAL_REQUIREMENTS).exists()

    def test_copy_skips_heavy_and_vcs_dirs(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        with Workspace(project, "python") as ws:
            assert (ws.copy_dir / "app.py").is_file()
            assert (ws.copy_dir / "requirements.txt").is_file()
            assert not (ws.copy_dir / ".git").exists()
            assert not (ws.copy_dir / "node_modules").exists()

    def test_workspace_removed_on_exit(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        with Workspace(project, "python") as ws:
            root = ws.root
            assert root is not None and root.exists()
        assert not root.exists()

    def test_keep_temp_preserves_workspace(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        with Workspace(project, "python", keep=True) as ws:
            root = ws.root
        assert root is not None and root.exists()

    def test_workspace_lives_outside_project(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        with Workspace(project, "python") as ws:
            assert ws.root is not None
            assert not ws.root.is_relative_to(project)

    def test_node_lockfile_deleted_only_in_copy(self, tmp_path: Path, monkeypatch) -> None:
        project = tmp_path / "nodeproj"
        project.mkdir()
        (project / "package.json").write_text('{"dependencies": {"left-pad": "^1.0.0"}}')
        (project / "package-lock.json").write_text('{"lockfileVersion": 3, "packages": {}}')
        before = tree_digest(project)

        monkeypatch.setattr(Workspace, "_install", lambda self, pins: None)
        monkeypatch.setattr(Workspace, "_run_test", lambda self, cmd, env: False)

        with Workspace(project, "node") as ws:
            assert ws.run_trial({"left-pad": "1.3.0"}, "npm test") is False
            assert not (ws.copy_dir / "package-lock.json").exists()
            rewritten = (ws.copy_dir / "package.json").read_text()
            assert '"left-pad": "1.3.0"' in rewritten

        assert tree_digest(project) == before

    def test_install_failure_counts_as_failing_trial(self, tmp_path: Path, monkeypatch) -> None:
        from depbisect.errors import DepbisectError

        project = make_project(tmp_path)

        def boom(self, pins):
            raise DepbisectError("resolver conflict")

        monkeypatch.setattr(Workspace, "_install", boom)
        with Workspace(project, "python") as ws:
            assert ws.run_trial({"brokenlib": "1.0.0"}, "pytest") is False


class TestRealTrialOffline:
    def test_python_trial_installs_local_wheel_and_runs_test(self, tmp_path: Path) -> None:
        """End-to-end trial against a local wheel, no network.

        Builds a minimal wheel by hand, then lets Workspace create a real
        venv, install from --find-links with --no-index, and run a test
        that imports the package.
        """
        wheels = tmp_path / "wheels"
        wheels.mkdir()
        _build_wheel(wheels, "tinypkg", "1.0.0", "VALUE = 1\n")

        project = tmp_path / "proj"
        project.mkdir()
        (project / "requirements.txt").write_text("tinypkg==1.0.0\n")
        (project / "check.py").write_text(
            "import tinypkg\nraise SystemExit(0 if tinypkg.VALUE == 1 else 1)\n"
        )
        before = tree_digest(project)

        with Workspace(project, "python", no_index=True, find_links=[wheels], timeout=300) as ws:
            assert ws.run_trial({"tinypkg": "1.0.0"}, "python check.py") is True
            assert ws.run_trial({"tinypkg": "1.0.0"}, "python -c 'raise SystemExit(3)'") is False

        assert tree_digest(project) == before


def _build_wheel(directory: Path, name: str, version: str, init_source: str) -> Path:
    """Write a minimal but valid wheel file, offline."""
    import base64
    import hashlib
    import zipfile

    wheel_path = directory / f"{name}-{version}-py3-none-any.whl"
    dist_info = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": init_source,
        f"{dist_info}/METADATA": (f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: depbisect-tests\nRoot-Is-Purelib: true\n"
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


class TestGitNeverWrites:
    def test_read_at_ref_does_not_touch_worktree(self, tmp_path: Path) -> None:
        from depbisect.gitref import read_at_ref

        project = tmp_path / "repo"
        project.mkdir()
        _git(project, "init", "-q")
        (project / "requirements.txt").write_text("pkg==1.0.0\n")
        _git(project, "add", ".")
        _git(project, "commit", "-q", "-m", "good")
        (project / "requirements.txt").write_text("pkg==2.0.0\n")

        before = tree_digest(project)
        content = read_at_ref(project, "HEAD", "requirements.txt")
        assert content == "pkg==1.0.0\n"
        assert tree_digest(project) == before  # git show never checks out


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(cwd),
        },
    )
