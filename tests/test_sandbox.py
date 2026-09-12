"""The safety layer: the user's project must come out byte-identical."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from depbisect.errors import DepbisectError
from depbisect.sandbox import TRIAL_REQUIREMENTS, TrialOutcome, Workspace


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


class TestTrialOutcome:
    """An install failure and a test failure are different facts."""

    def test_uninstallable_is_not_a_test_failure(self, tmp_path: Path, monkeypatch) -> None:
        project = make_project(tmp_path)
        with Workspace(project, "python") as ws:

            def refuse(pins: dict[str, str]) -> Path:
                raise DepbisectError("no matching distribution")

            monkeypatch.setattr(ws, "_install", refuse)
            assert ws.try_trial({"brokenlib": "1.0.0"}, "true") is TrialOutcome.UNINSTALLABLE

    def test_run_trial_still_collapses_it_to_false_for_the_subset_stage(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        project = make_project(tmp_path)
        with Workspace(project, "python") as ws:

            def refuse(pins: dict[str, str]) -> Path:
                raise DepbisectError("no matching distribution")

            monkeypatch.setattr(ws, "_install", refuse)
            assert ws.run_trial({"brokenlib": "1.0.0"}, "true") is False
        assert "counting as FAIL" in capsys.readouterr().err

    def test_pass_and_fail_come_from_the_test_command(self, tmp_path: Path, monkeypatch) -> None:
        project = make_project(tmp_path)
        with Workspace(project, "python") as ws:
            monkeypatch.setattr(ws, "_install", lambda pins: None)
            assert ws.try_trial({}, "true") is TrialOutcome.PASS
            assert ws.try_trial({}, "false") is TrialOutcome.FAIL


def make_node_project(tmp_path: Path, dependency: str, version: str) -> Path:
    project = tmp_path / "nodeproj"
    project.mkdir()
    (project / "package.json").write_text(
        f'{{"name": "app", "version": "1.0.0", "dependencies": {{"{dependency}": "{version}"}}}}\n'
    )
    return project


NPM_INSTALL = ["npm", "install", "--no-audit", "--no-fund", "--loglevel=error"]


class TestNodeRegistryInstalls:
    def test_an_explicit_index_url_is_the_registry_npm_installs_from(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # The candidates were read from that URL, so the installs must come
        # from it too; otherwise a private or local registry would list
        # releases that npm then goes looking for somewhere else.
        project = make_node_project(tmp_path, "widget", "2.0.0")
        commands: list[list[str]] = []
        monkeypatch.setattr(Workspace, "_run", lambda self, cmd, *, cwd, what: commands.append(cmd))
        with Workspace(project, "node", index_url="http://127.0.0.1:4873/") as ws:
            assert ws.try_trial({"widget": "1.0.0"}, "true") is TrialOutcome.PASS
        assert commands == [[*NPM_INSTALL, "--registry", "http://127.0.0.1:4873/"]]

    def test_without_one_npm_keeps_its_configured_registry(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        project = make_node_project(tmp_path, "widget", "2.0.0")
        commands: list[list[str]] = []
        monkeypatch.setattr(Workspace, "_run", lambda self, cmd, *, cwd, what: commands.append(cmd))
        with Workspace(project, "node") as ws:
            assert ws.try_trial({"widget": "1.0.0"}, "true") is TrialOutcome.PASS
        assert commands == [NPM_INSTALL]

    def test_a_node_trial_installs_from_a_local_registry(self, tmp_path: Path, monkeypatch) -> None:
        """End to end against a registry on 127.0.0.1: real npm, no network.

        This is the path `--online --index-url` takes for a Node project,
        and the one part 5 of demo.sh relies on.
        """
        import json
        import shutil
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        import pytest

        if shutil.which("npm") is None:
            pytest.skip("needs npm on PATH")

        tarballs = {"1.0.0": _npm_tarball("tinypad", "1.0.0", "module.exports = 'one';\n")}
        prefix = "/tinypad/-/tinypad-"

        class Registry(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # name fixed by http.server
                if self.path == "/tinypad":
                    body = json.dumps(_packument("tinypad", tarballs, base_url)).encode()
                elif self.path.startswith(prefix) and self.path.endswith(".tgz"):
                    body = tarballs.get(self.path[len(prefix) : -len(".tgz")], b"")
                else:
                    body = b""
                self.send_response(200 if body else 404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Registry)
        base_url = f"http://127.0.0.1:{server.server_address[1]}/"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        monkeypatch.setenv("npm_config_cache", str(tmp_path / "npm-cache"))
        monkeypatch.setenv("npm_config_update_notifier", "false")

        project = make_node_project(tmp_path, "tinypad", "1.0.0")
        (project / "check.js").write_text("process.exit(require('tinypad') === 'one' ? 0 : 1);\n")
        before = tree_digest(project)
        try:
            with Workspace(project, "node", index_url=base_url, timeout=300) as ws:
                assert ws.try_trial({"tinypad": "1.0.0"}, "node check.js") is TrialOutcome.PASS
                # A version the registry never published will not install,
                # which is a third answer and not a failing test.
                outcome = ws.try_trial({"tinypad": "9.9.9"}, "node check.js")
                assert outcome is TrialOutcome.UNINSTALLABLE
        finally:
            server.shutdown()
            server.server_close()
        assert tree_digest(project) == before


class TestNodeTarballInstalls:
    def test_a_pin_a_tarball_holds_is_installed_from_that_tarball(
        self, tmp_path: Path, monkeypatch, write_package
    ) -> None:
        packs = tmp_path / "packs"
        tarball = write_package(packs / "widget-1.1.0.tgz", "widget", "1.1.0")
        project = make_node_project(tmp_path, "widget", "2.0.0")
        monkeypatch.setattr(Workspace, "_run", lambda self, cmd, *, cwd, what: None)
        with Workspace(project, "node", find_links=[packs]) as ws:
            ws.try_trial({"widget": "1.1.0"}, "true")
            held = json.loads((ws.copy_dir / "package.json").read_text())["dependencies"]
            ws.try_trial({"widget": "1.0.0"}, "true")
            not_held = json.loads((ws.copy_dir / "package.json").read_text())["dependencies"]
        assert held == {"widget": f"file:{tarball.resolve()}"}
        # No tarball holds 1.0.0, so npm is asked for the version as before.
        assert not_held == {"widget": "1.0.0"}

    def test_no_index_runs_npm_offline(self, tmp_path: Path, monkeypatch) -> None:
        project = make_node_project(tmp_path, "widget", "2.0.0")
        commands: list[list[str]] = []
        monkeypatch.setattr(Workspace, "_run", lambda self, cmd, *, cwd, what: commands.append(cmd))
        with Workspace(project, "node", no_index=True) as ws:
            assert ws.try_trial({"widget": "1.0.0"}, "true") is TrialOutcome.PASS
        assert commands == [[*NPM_INSTALL, "--offline"]]

    def test_an_offline_node_trial_installs_from_tarballs_alone(
        self, tmp_path: Path, monkeypatch, write_package
    ) -> None:
        """Real npm, --no-index, an empty npm cache, and a registry that refuses connections.

        Nothing but the tarballs can supply a package here, so a trial that
        passes proves the tarball was installed, and the two versions carry
        different code so each trial proves WHICH tarball.
        """
        if shutil.which("npm") is None:
            pytest.skip("needs npm on PATH")
        packs = tmp_path / "packs"
        write_package(packs / "tinypad-1.0.0.tgz", "tinypad", "1.0.0", "module.exports = 'one';\n")
        write_package(packs / "tinypad-1.1.0.tgz", "tinypad", "1.1.0", "module.exports = 'two';\n")
        monkeypatch.setenv("npm_config_cache", str(tmp_path / "empty-npm-cache"))
        monkeypatch.setenv("npm_config_registry", "http://127.0.0.1:9/")
        monkeypatch.setenv("npm_config_update_notifier", "false")

        project = make_node_project(tmp_path, "tinypad", "1.1.0")
        (project / "check.js").write_text("process.exit(require('tinypad') === 'one' ? 0 : 1);\n")
        before = tree_digest(project)
        with Workspace(project, "node", no_index=True, find_links=[packs], timeout=120) as ws:
            assert ws.try_trial({"tinypad": "1.0.0"}, "node check.js") is TrialOutcome.PASS
            assert ws.try_trial({"tinypad": "1.1.0"}, "node check.js") is TrialOutcome.FAIL
            # A version no tarball holds cannot come from anywhere, and
            # --offline makes npm say so at once rather than retry a registry.
            assert ws.try_trial({"tinypad": "9.9.9"}, "node check.js") is TrialOutcome.UNINSTALLABLE
            assert "ENOTCACHED" in (ws.last_install_error or "")
        assert tree_digest(project) == before


def _npm_tarball(name: str, version: str, index_js: str) -> bytes:
    """A minimal npm package tarball: everything under package/, gzipped."""
    import gzip
    import io
    import json
    import tarfile

    files = {
        "package/package.json": json.dumps({"name": name, "version": version, "main": "index.js"}),
        "package/index.js": index_js,
    }
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for arcname, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue(), mtime=0)


def _packument(name: str, tarballs: dict[str, bytes], base_url: str) -> dict[str, object]:
    """The package document a registry serves for ``tarballs``."""
    import base64

    versions = {
        version: {
            "name": name,
            "version": version,
            "dist": {
                "tarball": f"{base_url}{name}/-/{name}-{version}.tgz",
                "shasum": hashlib.sha1(data).hexdigest(),
                "integrity": "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode(),
            },
        }
        for version, data in tarballs.items()
    }
    return {"name": name, "dist-tags": {"latest": max(tarballs)}, "versions": versions}
