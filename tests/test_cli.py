"""CLI orchestration, exercised with a mocked trial runner (no installs)."""

import subprocess
from pathlib import Path

import pytest

from depbisect import candidates
from depbisect.cli import main
from depbisect.index import DistFile, IndexError_, Release
from depbisect.sandbox import TrialOutcome, Workspace


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
        assert "2 intermediate release(s) found locally" in out  # 1.1.0 and 1.2.0
        assert "Estimated test runs:" in out
        assert (project / "requirements.txt").read_text() == before

    def test_plan_without_local_candidates(self, project: Path, capsys) -> None:
        code = main(["run", "--test", "pytest", "-C", str(project), "--dry-run"])
        out = capsys.readouterr().out
        assert code == 0
        assert "no intermediate versions found" in out

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


def fake_trials(breaking: dict[str, str], uninstallable: set[tuple[str, str]] | None = None):
    """Trial runner stub: fails iff any breaking package is at its bad version.

    ``breaking`` maps package name to the first failing version.
    ``uninstallable`` is a set of (package, version) pairs that stand in
    for a release that will not install here, so the stub can answer
    UNINSTALLABLE without a network or a real interpreter.

    This patches ``try_trial``, the three-valued primitive, which leaves
    the real ``run_trial`` wrapper in the path the subset stage uses.
    """

    def try_trial(self: Workspace, pins: dict[str, str], test_cmd: str) -> TrialOutcome:
        from depbisect.versions import compare

        for pair in uninstallable or set():
            if pins.get(pair[0]) == pair[1]:
                return TrialOutcome.UNINSTALLABLE
        for name, first_bad in breaking.items():
            if name in pins and compare(pins[name], first_bad) >= 0:
                return TrialOutcome.FAIL
        return TrialOutcome.PASS

    return try_trial


class TestFullSessionMocked:
    def test_isolates_culprit_and_bisects_versions(
        self, project: Path, wheels: Path, capsys, monkeypatch
    ) -> None:
        monkeypatch.setattr(Workspace, "try_trial", fake_trials({"brokenlib": "2.0.0"}))
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
        monkeypatch.setattr(Workspace, "try_trial", fake_trials({"brokenlib": "2.0.0"}))
        code = main(["run", "--test", "pytest", "-C", str(project)])
        out = capsys.readouterr().out
        assert code == 0
        assert "Last passing:  1.0.0" in out
        assert "First failing: 2.0.0" in out
        assert "no intermediate versions were available" in out

    def test_interaction_reported_as_experimental(self, project: Path, capsys, monkeypatch) -> None:
        monkeypatch.setattr(
            Workspace,
            "try_trial",
            fake_trials({"brokenlib": "2.0.0", "okpkg": "1.1.0"}),
        )
        code = main(["run", "--test", "pytest", "-C", str(project)])
        out = capsys.readouterr().out
        assert code == 0
        assert "multiple packages (experimental)" in out
        assert "brokenlib" in out and "okpkg" in out

    def test_bad_state_passing_is_an_error(self, project: Path, capsys, monkeypatch) -> None:
        monkeypatch.setattr(Workspace, "try_trial", fake_trials({}))
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


def stub_index(monkeypatch, releases: list[Release] | Exception) -> None:
    """Replace the index client. Nothing in the CLI tests opens a socket."""

    def fetch(package: str, **kwargs: object) -> list[Release]:
        if isinstance(releases, Exception):
            raise releases
        return releases

    monkeypatch.setattr(candidates, "fetch_releases", fetch)


def rel(version: str, *, yanked: bool = False, filename: str | None = None) -> Release:
    """An index release publishing one file, a universal wheel by default."""
    name = filename or f"brokenlib-{version}-py3-none-any.whl"
    return Release(version=version, files=(DistFile(name, yanked, None),))


class TestVersionsSubcommand:
    def test_local_candidates(self, wheels: Path, capsys) -> None:
        code = main(
            [
                "versions",
                "brokenlib",
                "--from",
                "1.0.0",
                "--to",
                "2.0.0",
                "--find-links",
                str(wheels),
            ]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "source: local wheels" in out
        assert "1.0.0  (good)" in out
        assert "2.0.0  (bad)" in out
        assert "1.1.0" in out and "1.2.0" in out
        assert "at most 2 test run(s)" in out

    def test_online_candidates_and_exclusions(self, monkeypatch, capsys) -> None:
        stub_index(monkeypatch, [rel("1.1.0"), rel("1.5.0rc1"), rel("1.7.0", yanked=True)])
        code = main(["versions", "brokenlib", "--from", "1.0.0", "--to", "2.0.0", "--online"])
        out = capsys.readouterr().out
        assert code == 0
        assert "source: index https://pypi.org/simple/" in out
        # Every filtered release is named here, with the reason and the
        # flag that would put it back: this command exists to show the
        # candidate path AND what is missing from it.
        assert "Not bisected (2):" in out
        assert "1.5.0rc1  pre-release (--pre to include)" in out
        assert "1.7.0     yanked (--include-yanked to include)" in out
        assert "3 candidate version(s)" in out  # 1.0.0, 1.1.0, 2.0.0

    def test_a_release_with_no_distribution_for_this_platform_is_named(
        self, monkeypatch, capsys
    ) -> None:
        stub_index(
            monkeypatch,
            [rel("1.1.0"), rel("1.5.0", filename="brokenlib-1.5.0-cp38-cp38-win_amd64.whl")],
        )
        code = main(["versions", "brokenlib", "--from", "1.0.0", "--to", "2.0.0", "--online"])
        out = capsys.readouterr().out
        assert code == 0
        assert "1.5.0  no distribution for this platform" in out
        assert "this platform:" in out
        assert "3 candidate version(s)" in out

    def test_an_sdist_only_release_stays_a_candidate(self, monkeypatch, capsys) -> None:
        stub_index(monkeypatch, [rel("1.5.0", filename="brokenlib-1.5.0.tar.gz")])
        code = main(["versions", "brokenlib", "--from", "1.0.0", "--to", "2.0.0", "--online"])
        out = capsys.readouterr().out
        assert code == 0
        assert "Not bisected" not in out
        assert "3 candidate version(s)" in out

    def test_pre_flag_admits_prereleases(self, monkeypatch, capsys) -> None:
        stub_index(monkeypatch, [rel("1.5.0rc1")])
        code = main(
            ["versions", "brokenlib", "--from", "1.0.0", "--to", "2.0.0", "--online", "--pre"]
        )
        assert code == 0
        assert "1.5.0rc1" in capsys.readouterr().out

    def test_index_failure_is_fatal_here(self, monkeypatch, capsys) -> None:
        # For `versions` the index IS the command, unlike `run`.
        stub_index(monkeypatch, IndexError_("could not reach the index"))
        code = main(["versions", "brokenlib", "--from", "1.0.0", "--to", "2.0.0", "--online"])
        assert code == 2
        assert "index query failed" in capsys.readouterr().err

    def test_offline_by_default_makes_no_request(self, monkeypatch, capsys) -> None:
        def explode(*args: object, **kwargs: object) -> list[Release]:
            raise AssertionError("no index request may happen without --online")

        monkeypatch.setattr(candidates, "fetch_releases", explode)
        code = main(["versions", "brokenlib", "--from", "1.0.0", "--to", "2.0.0"])
        assert code == 0
        assert "no intermediate releases found" in capsys.readouterr().out


class TestIndexFlagValidation:
    def test_online_conflicts_with_no_index(self, project: Path, capsys) -> None:
        code = main(
            ["run", "--test", "pytest", "-C", str(project), "--dry-run", "--online", "--no-index"]
        )
        assert code == 2
        assert "contradict each other" in capsys.readouterr().err

    def test_index_url_conflicts_with_no_index(self, project: Path, capsys) -> None:
        code = main(
            [
                "run",
                "--test",
                "pytest",
                "-C",
                str(project),
                "--dry-run",
                "--no-index",
                "--index-url",
                "https://example.test/simple/",
            ]
        )
        assert code == 2
        assert "no meaning with --no-index" in capsys.readouterr().err

    def test_pre_requires_online(self, project: Path, capsys) -> None:
        code = main(["run", "--test", "pytest", "-C", str(project), "--dry-run", "--pre"])
        assert code == 2
        assert "need --online" in capsys.readouterr().err

    def test_online_rejected_for_node_projects(self, tmp_path: Path, capsys) -> None:
        node = tmp_path / "node"
        node.mkdir()
        (node / "package.json").write_text('{"dependencies": {"left-pad": "1.0.0"}}')
        code = main(["run", "--test", "npm test", "-C", str(node), "--dry-run", "--online"])
        assert code == 2
        assert "no npm registry support yet" in capsys.readouterr().err


class TestOnlineSession:
    def test_index_candidates_tighten_the_boundary(
        self, project: Path, capsys, monkeypatch
    ) -> None:
        # Nothing local at all: every intermediate release comes from the
        # index, which is the whole point of the feature.
        stub_index(monkeypatch, [rel("1.1.0"), rel("1.2.0")])
        monkeypatch.setattr(Workspace, "try_trial", fake_trials({"brokenlib": "2.0.0"}))
        code = main(["run", "--test", "pytest", "-C", str(project), "--online"])
        out = capsys.readouterr().out
        assert code == 0
        assert "Last passing:  1.2.0" in out
        assert "First failing: 2.0.0" in out
        assert "candidates from the package index" in out

    def test_uninstallable_release_is_skipped_and_named(
        self, project: Path, capsys, monkeypatch
    ) -> None:
        stub_index(monkeypatch, [rel("1.1.0"), rel("1.5.0")])
        monkeypatch.setattr(
            Workspace,
            "try_trial",
            fake_trials({"brokenlib": "2.0.0"}, uninstallable={("brokenlib", "1.5.0")}),
        )
        code = main(["run", "--test", "pytest", "-C", str(project), "--online"])
        out = capsys.readouterr().out
        assert code == 0
        assert "SKIP (will not install here)" in out
        # 1.5.0 must not be blamed for a regression it never ran.
        assert "First failing: 2.0.0" in out
        assert "could not be installed here and was skipped rather than blamed: 1.5.0" in out
        assert "not tight" in out

    def test_dead_index_warns_and_continues(
        self, project: Path, wheels: Path, capsys, monkeypatch
    ) -> None:
        stub_index(monkeypatch, IndexError_("could not reach https://pypi.org/simple/brokenlib/"))
        monkeypatch.setattr(Workspace, "try_trial", fake_trials({"brokenlib": "2.0.0"}))
        code = main(
            ["run", "--test", "pytest", "-C", str(project), "--online", "--find-links", str(wheels)]
        )
        captured = capsys.readouterr()
        assert code == 0
        assert "using local candidates only" in captured.err
        assert "Last passing:  1.2.0" in captured.out  # the local wheels still worked
