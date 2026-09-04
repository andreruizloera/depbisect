# Roadmap

Honest future work, none of it implemented yet.

## Candidate discovery

- Optional network mode (`--online`) that queries the PyPI JSON API or
  the npm registry for the full release list between good and bad, so
  version bisection covers every published release instead of only
  locally available distributions.
- Read candidate versions out of the pip and uv wheel caches, which are
  often already warm on developer machines and keep the offline
  guarantee.
- Yanked-release awareness when a network source is added.

## Ecosystems

- poetry.lock and Pipfile.lock parsers for Python.
- pnpm-lock.yaml and yarn.lock parsers for Node.
- Cargo.lock (Rust) and go.mod/go.sum (Go); the search core is
  ecosystem-agnostic already, only parsers and installers are needed.

## Search

- Real multi-dependency interaction search: after ddmin returns a set
  of size k > 1, bisect each member's versions while holding the
  others at their boundary, and test pairwise combinations. Today the
  set is reported as an experimental lead only.
- Parallel trials: independent subsets and version probes can run in
  separate workspaces at once; run count is the bottleneck.
- Retry-on-flake policy (run each trial up to N times, majority vote)
  to tolerate mildly flaky test suites.
- Cross-invocation trial cache keyed on the pin assignment, so an
  interrupted session resumes instead of restarting.

## Workflow

- `depbisect run` inside CI with `--json` output for machine-readable
  verdicts and a nonzero exit code contract.
- An `--install-cmd` override for projects that need more than a plain
  requirements install (editable installs, extras, compilers).
- git worktree mode that jointly bisects code and dependencies when
  the breaking commit mixes both.
- Shell completion and a `depbisect show` subcommand that re-prints the
  last verdict.
