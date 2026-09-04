"""CLI orchestration, exercised with a mocked trial runner (no installs)."""

import subprocess
from pathlib import Path

import pytest

from depbisect.cli import main
from depbisect.sandbox import Workspace


def git(cwd: Path, *args: str) -> None:
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


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Repo whose worktree bumps brokenlib 1.0.0 -> 2.0.0 and okpkg 1.0.0 -> 1.1.0."""
    proj = tmp_path / "proj"
    proj.mkdir()
    git(proj, "init", "-q")
    (proj / "requirements.txt").write_text("brokenlib==1.0.0\nokpkg==1.0.0\n")
    git(proj, "add", ".")
    git(proj, "commit", "-qm", "good pins")
    (proj / "requirements.txt").write_text("brokenlib==2.0.0\nokpkg==1.1.0\n")
    return proj


@pytest.fixture
def wheels(tmp_path: Path) -> Path:
    d = tmp_path / "wheels"
    d.mkdir()
    for name in [
        "brokenlib-1.0.0-py3-none-any.whl",
        "brokenlib-1.1.0-py3-none-any.whl",
        "brokenlib-1.2.0-py3-none-any.whl",
        "brokenlib-2.0.0-py3-none-any.whl",
        "okpkg-1.0.0-py3-none-any.whl",
        "okpkg-1.1.0-py3-none-any.whl",
    ]:
        (d / name).write_bytes(b"")
    return d


class TestDryRun:
    def test_plan_prints_and_mutates_nothing(self, project: Path, wheels: Path, capsys) -> None:
        before = (project / "requirements.txt").read_text()
        code = main(
            [
                "run",
                "--test",
                "pytest",
                "-C",
                str(project),
                "--find-links",
                str(wheels),
                "--dry-run",
            ]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "depbisect plan (dry run)" in out
        assert "brokenlib" in out and "1.0.0 -> 2.0.0" in out
        assert "okpkg" in out and "1.0.0 -> 1.1.0" in out
        assert "2 intermediate version(s)" in out  # 1.1.0 and 1.2.0
        assert "Estimated test runs:" in out
        assert (project / "requirements.txt").read_text() == before

    def test_plan_without_local_candidates(self, project: Path, capsys) -> None:
        code = main(["run", "--test", "pytest", "-C", str(project), "--dry-run"])
        out = capsys.readouterr().out
        assert code == 0
        assert "no intermediate versions found locally" in out

    def test_explicit_refs(self, project: Path, capsys) -> None:
        git(project, "commit", "-aqm", "bad pins")
        code = main(
            [
                "run",
                "--test",
                "pytest",
                "-C",
                str(project),
                "--good-ref",
                "HEAD~1",
                "--bad-ref",
                "HEAD",
                "--dry-run",
            ]
        )
        assert code == 0
        assert "HEAD~1" in capsys.readouterr().out


class TestErrors:
    def test_no_manifest(self, tmp_path: Path, capsys) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        code = main(["run", "--test", "pytest", "-C", str(empty), "--dry-run"])
        assert code == 2
        assert "no supported manifest" in capsys.readouterr().err

    def test_no_changes(self, project: Path, capsys) -> None:
        (project / "requirements.txt").write_text("brokenlib==1.0.0\nokpkg==1.0.0\n")
        code = main(
            ["run", "--test", "pytest", "-C", str(project), "--good-ref", "HEAD", "--dry-run"]
        )
        assert code == 2
        assert "no dependency changes" in capsys.readouterr().err

    def test_unchanged_worktree_cannot_autodetect(self, project: Path, capsys) -> None:
        (project / "requirements.txt").write_text("brokenlib==1.0.0\nokpkg==1.0.0\n")
        code = main(["run", "--test", "pytest", "-C", str(project), "--dry-run"])
        assert code == 2
        assert "could not auto-detect a good ref" in capsys.readouterr().err

    def test_not_a_repo_without_good_ref(self, tmp_path: Path, capsys) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "requirements.txt").write_text("a==1\n")
        code = main(["run", "--test", "pytest", "-C", str(plain), "--dry-run"])
        assert code == 2
        assert "not a git repository" in capsys.readouterr().err

    def test_missing_directory(self, tmp_path: Path, capsys) -> None:
        code = main(["run", "--test", "pytest", "-C", str(tmp_path / "nope"), "--dry-run"])
        assert code == 2
        assert "not a directory" in capsys.readouterr().err


def fake_trials(breaking: dict[str, str]):
    """Trial runner stub: fails iff any breaking package is at its bad version.

    ``breaking`` maps package name to the first failing version.
    """

    def run_trial(self: Workspace, pins: dict[str, str], test_cmd: str) -> bool:
        from depbisect.versions import compare

        for name, first_bad in breaking.items():
            if name in pins and compare(pins[name], first_bad) >= 0:
                return False
        return True

    return run_trial


class TestFullSessionMocked:
    def test_isolates_culprit_and_bisects_versions(
        self, project: Path, wheels: Path, capsys, monkeypatch
    ) -> None:
        monkeypatch.setattr(Workspace, "run_trial", fake_trials({"brokenlib": "2.0.0"}))
        code = main(
            [
                "run",
                "--test",
                "pytest",
                "-C",
                str(project),
                "--find-links",
                str(wheels),
            ]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "Dependency regression isolated" in out
        assert "Package:       brokenlib" in out
        assert "Last passing:  1.2.0" in out
        assert "First failing: 2.0.0" in out
        assert "Runs:          5" in out

    def test_without_candidates_bisects_known_versions_only(
        self, project: Path, capsys, monkeypatch
    ) -> None:
        monkeypatch.setattr(Workspace, "run_trial", fake_trials({"brokenlib": "2.0.0"}))
        code = main(["run", "--test", "pytest", "-C", str(project)])
        out = capsys.readouterr().out
        assert code == 0
        assert "Last passing:  1.0.0" in out
        assert "First failing: 2.0.0" in out
        assert "no intermediate versions were available locally" in out

    def test_interaction_reported_as_experimental(self, project: Path, capsys, monkeypatch) -> None:
        monkeypatch.setattr(
            Workspace,
            "run_trial",
            fake_trials({"brokenlib": "2.0.0", "okpkg": "1.1.0"}),
        )
        code = main(["run", "--test", "pytest", "-C", str(project)])
        out = capsys.readouterr().out
        assert code == 0
        assert "multiple packages (experimental)" in out
        assert "brokenlib" in out and "okpkg" in out

    def test_bad_state_passing_is_an_error(self, project: Path, capsys, monkeypatch) -> None:
        monkeypatch.setattr(Workspace, "run_trial", fake_trials({}))
        code = main(["run", "--test", "pytest", "-C", str(project)])
        assert code == 2
        assert "passes with the bad dependency state" in capsys.readouterr().err

    def test_good_state_failing_is_an_error(self, project: Path, capsys, monkeypatch) -> None:
        monkeypatch.setattr(Workspace, "run_trial", lambda self, pins, cmd: False)
        code = main(["run", "--test", "pytest", "-C", str(project)])
        assert code == 2
        assert "regression is probably in your code" in capsys.readouterr().err

    def test_added_dependency_reported(self, project: Path, capsys, monkeypatch) -> None:
        (project / "requirements.txt").write_text("brokenlib==1.0.0\nokpkg==1.0.0\nnewdep==0.1.0\n")

        def run_trial(self: Workspace, pins: dict[str, str], test_cmd: str) -> bool:
            return "newdep" not in pins

        monkeypatch.setattr(Workspace, "run_trial", run_trial)
        code = main(["run", "--test", "pytest", "-C", str(project)])
        out = capsys.readouterr().out
        assert code == 0
        assert "newdep" in out
        assert "added" in out
