"""depbisect command-line interface and session orchestration."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from depbisect import __version__
from depbisect.bisector import (
    bisect_versions,
    estimate_runs,
    minimize_breaking_set,
)
from depbisect.diffing import ChangedDep, diff_states
from depbisect.errors import DepbisectError
from depbisect.gitref import (
    WORKTREE,
    guess_good_ref,
    is_git_repo,
    read_at_ref,
    short_ref,
)
from depbisect.manifests import DepState, detect_ecosystem, parse_state, pick_source
from depbisect.sandbox import Workspace
from depbisect.versions import VersionParseError, local_versions, version_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="depbisect",
        description="git bisect for dependency versions",
    )
    parser.add_argument("--version", action="version", version=f"depbisect {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="isolate a dependency regression")
    run.add_argument(
        "--test",
        required=True,
        metavar="CMD",
        help='test command that fails now, e.g. "pytest" or "npm test"',
    )
    run.add_argument(
        "--good-ref",
        metavar="REF",
        help="git ref where deps were known good (default: auto-detect the "
        "newest commit whose manifest differs from the bad state)",
    )
    run.add_argument(
        "--bad-ref",
        metavar="REF",
        help="git ref of the known-bad dep state (default: the working tree)",
    )
    run.add_argument(
        "-C",
        "--directory",
        default=".",
        metavar="DIR",
        help="project directory (default: current directory)",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan (changed deps, candidates, run estimate) "
        "and exit without installing or running anything",
    )
    run.add_argument(
        "--find-links",
        action="append",
        default=[],
        metavar="DIR",
        help="local directory of wheels/sdists used both as install source "
        "and as the offline candidate-version list (repeatable)",
    )
    run.add_argument(
        "--no-index",
        action="store_true",
        help="forbid the package index entirely; install only from --find-links",
    )
    run.add_argument(
        "--timeout",
        type=int,
        default=600,
        metavar="SECONDS",
        help="per-command timeout for installs and tests (default: 600)",
    )
    run.add_argument(
        "--keep-temp", action="store_true", help="keep the temporary workspace for inspection"
    )
    run.add_argument(
        "--verbose", action="store_true", help="stream install/test output instead of capturing it"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_session(args)
    except DepbisectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


def run_session(args: argparse.Namespace) -> int:
    project = Path(args.directory).resolve()
    if not project.is_dir():
        raise DepbisectError(f"{project} is not a directory")

    ecosystem = detect_ecosystem(project)
    source = pick_source(project, ecosystem)
    bad_ref = args.bad_ref or WORKTREE
    if (args.good_ref or bad_ref != WORKTREE) and not is_git_repo(project):
        raise DepbisectError(f"{project} is not a git repository, so refs cannot be used")

    bad_state = _read_state(project, bad_ref, source, ecosystem)
    good_ref = args.good_ref
    if good_ref is None:
        if not is_git_repo(project):
            raise DepbisectError(
                "no --good-ref given and the project is not a git repository; "
                "pass --good-ref or run inside a repo"
            )
        good_ref = guess_good_ref(project, source)
        if good_ref is None:
            raise DepbisectError(
                f"could not auto-detect a good ref: no commit in the last 100 touching "
                f"{source} differs from the current state; pass --good-ref explicitly"
            )
    good_state = _read_state(project, good_ref, source, ecosystem)

    changed = diff_states(good_state, bad_state)
    if not changed:
        raise DepbisectError(
            f"no dependency changes found in {source} between "
            f"{short_ref(project, good_ref)} and {short_ref(project, bad_ref)}"
        )

    find_links = [Path(d) for d in args.find_links]
    candidates = _candidate_paths(changed, find_links, ecosystem)

    if args.dry_run:
        _print_plan(project, ecosystem, source, good_ref, bad_ref, args.test, changed, candidates)
        return 0

    return _bisect(project, ecosystem, good_state, bad_state, changed, candidates, args)


def _read_state(project: Path, ref: str, source: str, ecosystem: str) -> DepState:
    content = read_at_ref(project, ref, source)
    return parse_state(source, content, ecosystem)


def _candidate_paths(
    changed: list[ChangedDep], find_links: list[Path], ecosystem: str
) -> dict[str, list[str]]:
    """Offline candidate version path per version-changed dependency.

    Candidates come only from local dist directories (--find-links).
    When none are found, the path is just [good, bad]: depbisect then
    bisects between the two known versions only, and says so.
    """
    paths: dict[str, list[str]] = {}
    for dep in changed:
        if dep.kind != "changed":
            continue
        assert dep.good is not None and dep.bad is not None
        available = local_versions(dep.name, find_links) if ecosystem == "python" else []
        try:
            paths[dep.name] = version_path(dep.good, dep.bad, available)
        except VersionParseError:
            paths[dep.name] = [dep.good, dep.bad]
    return paths


def _print_plan(
    project: Path,
    ecosystem: str,
    source: str,
    good_ref: str,
    bad_ref: str,
    test_cmd: str,
    changed: list[ChangedDep],
    candidates: dict[str, list[str]],
) -> None:
    print("depbisect plan (dry run)\n")
    print(f"  Project:   {project} ({ecosystem})")
    print(f"  Manifest:  {source}")
    print(f"  Good ref:  {short_ref(project, good_ref)}")
    print(f"  Bad state: {short_ref(project, bad_ref)}")
    print(f"  Test:      {test_cmd}\n")
    print(f"  Changed dependencies ({len(changed)}):")
    width = max(len(dep.name) for dep in changed)
    max_candidates = 2
    for dep in changed:
        line = f"    {dep.name:<{width}}  {dep.describe()}"
        path = candidates.get(dep.name)
        if path is not None:
            interior = len(path) - 2
            if interior > 0:
                line += f"   ({interior} intermediate version(s) available locally)"
                max_candidates = max(max_candidates, len(path))
            else:
                line += "   (no intermediate versions found locally)"
        print(line)
    lo, hi = estimate_runs(len(changed), max_candidates)
    print(f"\n  Estimated test runs: {lo} to {hi}")
    print("  Dry run: nothing was installed and no files were modified.")


def _bisect(
    project: Path,
    ecosystem: str,
    good_state: DepState,
    bad_state: DepState,
    changed: list[ChangedDep],
    candidates: dict[str, list[str]],
    args: argparse.Namespace,
) -> int:
    total_runs = 0
    find_links = [Path(d) for d in args.find_links]

    with Workspace(
        project,
        ecosystem,
        keep=args.keep_temp,
        no_index=args.no_index,
        find_links=find_links,
        timeout=args.timeout,
        verbose=args.verbose,
    ) as ws:
        print(f"workspace: {ws.copy_dir} (your project is not touched)")

        def pins_with_reverted(names: frozenset[str]) -> dict[str, str]:
            pins = dict(bad_state.pins)
            for name in names:
                good_version = good_state.pins.get(name)
                if good_version is None:
                    pins.pop(name, None)  # dep was added in bad; revert = drop it
                else:
                    pins[name] = good_version
            return pins

        # Baselines: the bad state must fail and the good state must pass,
        # otherwise the premise "a dependency change broke it" is wrong.
        print("baseline: testing bad dependency state ... ", end="", flush=True)
        bad_passes = ws.run_trial(pins_with_reverted(frozenset()), args.test)
        total_runs += 1
        print("PASS" if bad_passes else "FAIL")
        if bad_passes:
            raise DepbisectError("the test passes with the bad dependency state; nothing to bisect")

        all_names = frozenset(dep.name for dep in changed)
        print("baseline: testing good dependency state ... ", end="", flush=True)
        good_passes = ws.run_trial(pins_with_reverted(all_names), args.test)
        total_runs += 1
        print("PASS" if good_passes else "FAIL")
        if not good_passes:
            raise DepbisectError(
                "the test still fails with all changed dependencies reverted to their "
                "good versions; the regression is probably in your code or environment, "
                "not in these dependency changes (try git bisect on the code instead)"
            )

        # Stage 1: shrink the changed set to the smallest reverting set.
        def subset_oracle(names: frozenset[str]) -> bool:
            label = ", ".join(sorted(names))
            print(f"subset:   reverting {{{label}}} ... ", end="", flush=True)
            passed = ws.run_trial(pins_with_reverted(names), args.test)
            print("PASS" if passed else "FAIL")
            return passed

        subset = minimize_breaking_set(changed, subset_oracle)
        total_runs += subset.runs

        if subset.interaction:
            _report_interaction(subset.culprits, args.test, total_runs)
            return 0

        culprit = subset.culprits[0]
        if culprit.kind != "changed":
            _report_add_remove(culprit, args.test, total_runs)
            return 0

        # Stage 2: bisect the culprit's candidate versions.
        path = candidates[culprit.name]

        def version_oracle(version: str) -> bool:
            print(f"bisect:   {culprit.name}=={version} ... ", end="", flush=True)
            pins = pins_with_reverted(all_names - {culprit.name})
            pins[culprit.name] = version
            passed = ws.run_trial(pins, args.test)
            print("PASS" if passed else "FAIL")
            return passed

        result = bisect_versions(path, version_oracle)
        total_runs += result.runs

        _report_success(
            culprit.name,
            result.last_passing,
            result.first_failing,
            args.test,
            total_runs,
            len(path),
        )
        if args.keep_temp:
            print(f"\nworkspace kept at {ws.copy_dir}")
    return 0


def _report_success(
    package: str,
    last_passing: str,
    first_failing: str,
    test_cmd: str,
    runs: int,
    path_len: int,
) -> None:
    print("\nDependency regression isolated\n")
    print(f"  Package:       {package}")
    print(f"  Last passing:  {last_passing}")
    print(f"  First failing: {first_failing}")
    print(f"  Test:          {test_cmd}")
    print(f"  Runs:          {runs}")
    if path_len <= 2:
        print(
            "\n  note: no intermediate versions were available locally, so this "
            "bisected only between the two known versions. Releases between "
            f"{last_passing} and {first_failing} were not tested."
        )


def _report_interaction(culprits: list[ChangedDep], test_cmd: str, runs: int) -> None:
    print("\nDependency regression involves multiple packages (experimental)\n")
    for dep in culprits:
        print(f"  Package:  {dep.name}  {dep.describe()}")
    print(f"  Test:     {test_cmd}")
    print(f"  Runs:     {runs}")
    print(
        "\n  No single dependency explains the failure: the test only passes when "
        "all of the above are reverted together. Interaction search is minimal "
        "for now; treat this set as a lead, not a verdict."
    )


def _report_add_remove(dep: ChangedDep, test_cmd: str, runs: int) -> None:
    print("\nDependency regression isolated\n")
    print(f"  Package:  {dep.name} ({dep.kind} in the bad state: {dep.describe()})")
    print(f"  Test:     {test_cmd}")
    print(f"  Runs:     {runs}")


if __name__ == "__main__":
    raise SystemExit(main())
