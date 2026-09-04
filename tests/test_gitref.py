"""Reading manifests at refs and auto-detecting the good ref."""

import subprocess
from pathlib import Path

import pytest

from depbisect.errors import DepbisectError
from depbisect.gitref import WORKTREE, guess_good_ref, is_git_repo, read_at_ref


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(cwd),
        },
    )
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    project = tmp_path / "repo"
    project.mkdir()
    git(project, "init", "-q")
    (project / "requirements.txt").write_text("pkg==1.0.0\n")
    git(project, "add", ".")
    git(project, "commit", "-q", "-m", "pin 1.0.0")
    return project


class TestReadAtRef:
    def test_worktree_sentinel(self, repo: Path) -> None:
        (repo / "requirements.txt").write_text("pkg==9.9.9\n")
        assert read_at_ref(repo, WORKTREE, "requirements.txt") == "pkg==9.9.9\n"

    def test_committed_ref(self, repo: Path) -> None:
        (repo / "requirements.txt").write_text("pkg==2.0.0\n")
        git(repo, "commit", "-aqm", "pin 2.0.0")
        assert read_at_ref(repo, "HEAD~1", "requirements.txt") == "pkg==1.0.0\n"
        assert read_at_ref(repo, "HEAD", "requirements.txt") == "pkg==2.0.0\n"

    def test_subdirectory_project(self, repo: Path) -> None:
        sub = repo / "service"
        sub.mkdir()
        (sub / "requirements.txt").write_text("svc==1.0\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "add service")
        assert read_at_ref(sub, "HEAD", "requirements.txt") == "svc==1.0\n"

    def test_missing_at_ref(self, repo: Path) -> None:
        with pytest.raises(DepbisectError):
            read_at_ref(repo, "HEAD", "nonexistent.txt")

    def test_bad_ref(self, repo: Path) -> None:
        with pytest.raises(DepbisectError):
            read_at_ref(repo, "no-such-ref", "requirements.txt")


class TestGuessGoodRef:
    def test_dirty_worktree_picks_head(self, repo: Path) -> None:
        (repo / "requirements.txt").write_text("pkg==2.0.0\n")
        ref = guess_good_ref(repo, "requirements.txt")
        assert ref is not None
        assert read_at_ref(repo, ref, "requirements.txt") == "pkg==1.0.0\n"

    def test_clean_worktree_picks_previous_touching_commit(self, repo: Path) -> None:
        (repo / "requirements.txt").write_text("pkg==2.0.0\n")
        git(repo, "commit", "-aqm", "pin 2.0.0")
        git(repo, "commit", "-q", "--allow-empty", "-m", "unrelated")
        ref = guess_good_ref(repo, "requirements.txt")
        assert ref is not None
        assert read_at_ref(repo, ref, "requirements.txt") == "pkg==1.0.0\n"

    def test_no_differing_history(self, repo: Path) -> None:
        assert guess_good_ref(repo, "requirements.txt") is None


class TestIsGitRepo:
    def test_yes(self, repo: Path) -> None:
        assert is_git_repo(repo)

    def test_no(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert not is_git_repo(plain)
