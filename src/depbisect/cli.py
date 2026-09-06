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
from depbisect.candidates import CandidateSet, build_candidates
from depbisect.diffing import ChangedDep, diff_states
from depbisect.errors import DepbisectError
from depbisect.gitref import (
    WORKTREE,
    guess_good_ref,
    is_git_repo,
    read_at_ref,
    short_ref,
)
from depbisect.index import DEFAULT_INDEX_URL
from depbisect.manifests import DepState, detect_ecosystem, parse_state, pick_source
from depbisect.sandbox import TrialOutcome, Workspace


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
    _add_index_options(run)
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

    versions = sub.add_parser(
        "versions",
        help="show the candidate versions a bisection would walk, without running anything",
    )
    versions.add_argument("package", help="package name")
    versions.add_argument(
        "--from", dest="good", required=True, metavar="VERSION", help="known-good"
    )
    versions.add_argument("--to", dest="bad", required=True, metavar="VERSION", help="known-bad")
    versions.add_argument(
        "--find-links",
        action="append",
        default=[],
        metavar="DIR",
        help="local directory of wheels/sdists to draw candidates from (repeatable)",
    )
    _add_index_options(versions)
    versions.add_argument(
        "--timeout",
        type=int,
        default=30,
        metavar="SECONDS",
        help="index request timeout (default: 30)",
    )
    return parser


def _add_index_options(parser: argparse.ArgumentParser) -> None:
    """Flags shared by ``run`` and ``versions``. Network is opt-in."""
    parser.add_argument(
        "--online",
        action="store_true",
        help="query the package index for the releases between the good and bad "
        "versions (Python only). Off by default: without it depbisect makes no "
        "network request of its own",
    )
    parser.add_argument(
        "--index-url",
        metavar="URL",
        help=f"simple-index base URL for --online and for installs (default: {DEFAULT_INDEX_URL})",
    )
    parser.add_argument(
        "--pre",
        action="store_true",
        help="include pre-releases (alpha, beta, rc, dev) from the index",
    )
    parser.add_argument(
        "--include-yanked",
        action="store_true",
        help="include yanked releases from the index",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "versions":
            return show_versions(args)
        return run_session(args)
    except DepbisectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


TRIAL_PYTHON = sys.version_info[:3]


def _check_index_options(args: argparse.Namespace, ecosystem: str | None = None) -> None:
    """Reject flag combinations that cannot mean anything, before any work."""
    if getattr(args, "no_index", False):
        if args.online:
            raise DepbisectError(
                "--online and --no-index contradict each other: --online finds releases "
                "at the index and --no-index forbids installing from it, so nothing "
                "found could be tested"
            )
        if args.index_url:
            raise DepbisectError("--index-url has no meaning with --no-index")
    if args.online and ecosystem == "node":
        raise DepbisectError(
            "--online reads a Python simple index; there is no npm registry support yet, "
            "so Node candidate versions still come from --find-links only"
        )
    if (args.pre or args.include_yanked) and not args.online:
        raise DepbisectError(
            "--pre and --include-yanked filter the index candidate list, so they need "
            "--online; versions found in a --find-links directory are never filtered"
        )


def show_versions(args: argparse.Namespace) -> int:
    """``depbisect versions``: print the path a bisection would walk."""
    _check_index_options(args)
    candidates = build_candidates(
        args.package,
        args.good,
        args.bad,
        find_links=[Path(d) for d in args.find_links],
        online=args.online,
        index_url=args.index_url or DEFAULT_INDEX_URL,
        python=TRIAL_PYTHON,
        allow_pre=args.pre,
        allow_yanked=args.include_yanked,
        timeout=args.timeout,
    )
    if candidates.note is not None:
        raise DepbisectError(f"index query failed: {candidates.note}")

    where = _source_phrase(candidates, args.index_url or DEFAULT_INDEX_URL)
    print(f"{args.package}: {len(candidates.path)} candidate version(s) to bisect ({where})\n")
    for version in candidates.path:
        label = ""
        if version == candidates.path[0]:
            label = "  (good)"
        elif version == candidates.path[-1]:
            label = "  (bad)"
        print(f"  {version}{label}")
    exclusions = candidates.describe_exclusions(TRIAL_PYTHON if args.online else None)
    print()
    if exclusions:
        print(f"  {exclusions}")
    print(f"  Bisection would need at most {candidates.max_runs()} test run(s).")
    return 0


def _source_phrase(candidates: CandidateSet, index_url: str) -> str:
    if candidates.source == "index":
        return f"source: index {index_url}"
    if candidates.source == "index and local":
        return f"source: index {index_url} and local wheels"
    if candidates.source == "local":
        return "source: local wheels"
    return "no intermediate releases found"


def run_session(args: argparse.Namespace) -> int:
    project = Path(args.directory).resolve()
    if not project.is_dir():
        raise DepbisectError(f"{project} is not a directory")

    ecosystem = detect_ecosystem(project)
    _check_index_options(args, ecosystem)
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
    candidates = _candidate_paths(changed, find_links, ecosystem, args)

    if args.dry_run:
        _print_plan(project, ecosystem, source, good_ref, bad_ref, args.test, changed, candidates)
        return 0

    return _bisect(project, ecosystem, good_state, bad_state, changed, candidates, args)


def _read_state(project: Path, ref: str, source: str, ecosystem: str) -> DepState:
    content = read_at_ref(project, ref, source)
    return parse_state(source, content, ecosystem)


def _candidate_paths(
    changed: list[ChangedDep],
    find_links: list[Path],
    ecosystem: str,
    args: argparse.Namespace,
) -> dict[str, CandidateSet]:
    """Candidate version path per version-changed dependency.

    Candidates come from local dist directories (--find-links) and, with
    --online, from the package index. When neither yields anything the
    path is just [good, bad]: depbisect then bisects between the two
    known versions only, and says so in the report.
    """
    paths: dict[str, CandidateSet] = {}
    for dep in changed:
        if dep.kind != "changed":
            continue
        assert dep.good is not None and dep.bad is not None
        candidates = build_candidates(
            dep.name,
            dep.good,
            dep.bad,
            find_links=find_links,
            online=args.online and ecosystem == "python",
            index_url=args.index_url or DEFAULT_INDEX_URL,
            python=TRIAL_PYTHON,
            allow_pre=args.pre,
            allow_yanked=args.include_yanked,
        )
        if candidates.note is not None:
            # A dead index is not a dead session: say so and carry on with
            # whatever local candidates there are.
            print(
                f"warning: index query for {dep.name} failed, using local candidates only: "
                f"{candidates.note}",
                file=sys.stderr,
            )
        paths[dep.name] = candidates
    return paths


def _print_plan(
    project: Path,
    ecosystem: str,
    source: str,
    good_ref: str,
    bad_ref: str,
    test_cmd: str,
    changed: list[ChangedDep],
    candidates: dict[str, CandidateSet],
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
        found = candidates.get(dep.name)
        if found is not None:
            if found.interior > 0:
                where = {
                    "index": "from the index",
                    "index and local": "from the index and local wheels",
                    "local": "found locally",
                }[found.source]
                line += f"   ({found.interior} intermediate release(s) {where})"
                max_candidates = max(max_candidates, len(found.path))
            else:
                line += "   (no intermediate versions found)"
        print(line)
        exclusions = found.describe_exclusions() if found else None
        if exclusions:
            print(f"    {'':<{width}}  {exclusions.lower()}")
    lo, hi = estimate_runs(len(changed), max_candidates)
    print(f"\n  Estimated test runs: {lo} to {hi}")
    print("  Dry run: nothing was installed and no files were modified.")


def _bisect(
    project: Path,
    ecosystem: str,
    good_state: DepState,
    bad_state: DepState,
    changed: list[ChangedDep],
    candidates: dict[str, CandidateSet],
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
        index_url=args.index_url,
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
        candidate_set = candidates[culprit.name]

        def version_oracle(version: str) -> bool | None:
            print(f"bisect:   {culprit.name}=={version} ... ", end="", flush=True)
            pins = pins_with_reverted(all_names - {culprit.name})
            pins[culprit.name] = version
            outcome = ws.try_trial(pins, args.test)
            if outcome is TrialOutcome.UNINSTALLABLE:
                print("SKIP (will not install here)")
                return None
            passed = outcome is TrialOutcome.PASS
            print("PASS" if passed else "FAIL")
            return passed

        result = bisect_versions(candidate_set.path, version_oracle)
        total_runs += result.runs

        _report_success(
            culprit.name,
            result.last_passing,
            result.first_failing,
            args.test,
            total_runs,
            candidate_set,
            result.skipped,
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
    candidates: CandidateSet,
    skipped: list[str],
) -> None:
    print("\nDependency regression isolated\n")
    print(f"  Package:       {package}")
    print(f"  Last passing:  {last_passing}")
    print(f"  First failing: {first_failing}")
    print(f"  Test:          {test_cmd}")
    print(f"  Runs:          {runs}")
    sources = {
        "index": "candidates from the package index",
        "index and local": "candidates from the package index and local wheels",
        "local": "candidates from local wheels",
        "none": "no intermediate candidates",
    }
    print(f"  Searched:      {len(candidates.path)} version(s), {sources[candidates.source]}")

    # Everything below is about how tight the boundary actually is. A
    # bisection that says "3.0.3 to 3.1.4" and a bisection that says
    # "3.1.0 to 3.1.1" are different answers, and the difference is
    # entirely in what was available to test.
    if candidates.interior == 0:
        print(
            "\n  note: no intermediate versions were available, so this bisected only "
            f"between the two known versions. Releases between {last_passing} and "
            f"{first_failing} were not tested."
        )
        if candidates.source == "none" and not candidates.note:
            print("        --online would look them up at the package index.")
    if skipped:
        one = len(skipped) == 1
        print(
            f"\n  note: {len(skipped)} {'release' if one else 'releases'} between "
            f"{last_passing} and {first_failing} could not be installed here and "
            f"{'was' if one else 'were'} skipped rather than blamed: " + ", ".join(skipped)
        )
        print("        the boundary above is therefore not tight.")
    exclusions = candidates.describe_exclusions()
    if exclusions:
        print(f"\n  note: {exclusions.lower()}")


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
