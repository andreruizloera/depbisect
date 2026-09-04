# depbisect

[![CI](https://github.com/andreruizloera/depbisect/actions/workflows/ci.yml/badge.svg)](https://github.com/andreruizloera/depbisect/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

git bisect for dependency versions.

Your tests broke after a dependency bump and you do not know which
package did it. depbisect diffs your manifest against the last known
good state, delta-debugs the changed set down to the culprit package,
then binary-searches its versions to the exact breaking release. Every
install and test run happens in a throwaway copy; your project is never
modified.

## Quickstart

```sh
git clone https://github.com/andreruizloera/depbisect
cd depbisect
./demo.sh
```

The demo (fully offline, local wheels only) breaks a tiny app with a
bad dependency bump, then lets depbisect find the culprit:

```text
$ cat requirements.txt
brokenlib==2.0.0
okpkg==1.1.0

$ depbisect run --test "python test_app.py" --no-index --find-links examples/demo/wheels
workspace: /var/folders/.../depbisect-21f0f62q/project (your project is not touched)
baseline: testing bad dependency state ... FAIL
baseline: testing good dependency state ... PASS
subset:   reverting {brokenlib} ... PASS
bisect:   brokenlib==1.1.0 ... PASS
bisect:   brokenlib==1.2.0 ... PASS

Dependency regression isolated

  Package:       brokenlib
  Last passing:  1.2.0
  First failing: 2.0.0
  Test:          python test_app.py
  Runs:          5
```

Two dependencies were bumped at once; depbisect reverts subsets to
find that brokenlib alone explains the failure, then bisects the
releases between 1.0.0 and 2.0.0 to the exact breaking one. And
`--dry-run` shows the plan first, touching nothing:

```text
$ depbisect run --test "python test_app.py" --find-links examples/demo/wheels --dry-run
depbisect plan (dry run)

  Project:   /tmp/demo-app (python)
  Manifest:  requirements.txt
  Good ref:  e69331ea55d5da3eaa365584e8ec0327c8f15247 (e69331e)
  Bad state: working tree
  Test:      python test_app.py

  Changed dependencies (2):
    brokenlib  1.0.0 -> 2.0.0   (2 intermediate version(s) available locally)
    okpkg      1.0.0 -> 1.1.0   (no intermediate versions found locally)

  Estimated test runs: 4 to 14
  Dry run: nothing was installed and no files were modified.
```

## Why?

`git bisect` finds the commit that broke your code, but a dependency
regression often lands in a single commit: one lockfile bump touching
a dozen packages. The commit is obvious; the culprit inside it is not.
Rolling packages back one at a time by hand is slow and unsystematic.
depbisect does the two searches that actually answer the question:

1. Subset search (delta debugging) over the changed dependencies:
   which smallest set, reverted to its old versions, fixes the tests?
2. Version bisection for the culprit: between the good and bad
   versions, which release introduced the failure?

Both searches are driven by your own test command, so "broken" means
exactly what your test suite says it means.

## Installation

Python 3.12 or newer. No runtime dependencies.

```sh
uv tool install git+https://github.com/andreruizloera/depbisect
# or
pip install git+https://github.com/andreruizloera/depbisect
```

## Usage

```sh
# Zero config: bad state is your working tree, good state is
# auto-detected (the newest commit whose manifest differs).
depbisect run --test "pytest"

# Explicit refs:
depbisect run --good-ref HEAD~20 --bad-ref HEAD --test "pytest"

# Node projects:
depbisect run --test "npm test"

# See the plan without installing or running anything:
depbisect run --test "pytest" --dry-run
```

Options for `depbisect run`:

| Flag | Meaning |
| --- | --- |
| `--test CMD` | test command that fails now (required) |
| `--good-ref REF` | git ref of the known-good dependency state (default: auto-detect) |
| `--bad-ref REF` | git ref of the known-bad state (default: working tree) |
| `-C, --directory DIR` | project directory (default: `.`) |
| `--dry-run` | print changed deps, local candidates, and a run estimate; mutate nothing |
| `--find-links DIR` | local wheel/sdist directory; install source and offline candidate list (repeatable) |
| `--no-index` | never touch the package index; install only from `--find-links` |
| `--timeout SECONDS` | per-command timeout (default 600) |
| `--keep-temp` | keep the temporary workspace for inspection |
| `--verbose` | stream install and test output |

Supported manifests: `uv.lock`, `requirements.txt` (pinned), pinned
`pyproject.toml` dependencies for Python; `package.json` plus
`package-lock.json` (v1 to v3) for Node. Lockfiles win when several
are present.

## Safety guarantee

depbisect never modifies your project. Good-state manifests are read
with `git show`, never by checking anything out. Trials run in a
temporary copy of your tree (skipping `.git`, virtualenvs,
`node_modules`); every install goes into a fresh virtualenv inside
that copy, and the copy is deleted afterwards unless you pass
`--keep-temp`. The test suite verifies the original tree is
byte-identical after a run.

## Architecture

```
src/depbisect/
  cli.py        argument parsing and session orchestration
  manifests.py  lockfile/manifest parsers (uv.lock, requirements.txt,
                pyproject.toml, package-lock.json) -> {name: version}
  gitref.py     read manifests at git refs via git show; good-ref
                auto-detection
  diffing.py    changed-dependency set between two states
  bisector.py   pure search algorithms: ddmin subset minimization and
                version binary search, oracle-driven and fully testable
  versions.py   dependency-free version parser/comparator (PEP 440 and
                semver shapes), offline candidate discovery from local
                wheel directories
  sandbox.py    the safety layer: temp-copy workspace, fresh venvs,
                trial installs, test execution
```

The search core is pure: it sees only an oracle callable and returns a
result plus run counts, so the algorithm tests use mocked runners and
never install anything.

A session is: two baseline runs (bad state must fail, good state must
pass), then subset minimization, then version bisection of the single
culprit over whatever candidate list is derivable offline.

## Limitations

- Candidate versions come only from local wheel/sdist directories
  (`--find-links`); depbisect never queries an index. With no local
  candidates it bisects between the two known versions only and says
  so in the report.
- Multi-dependency interaction detection is experimental: when no
  single revert fixes the test, the minimal reverting set is reported
  as a lead, without per-package version bisection.
- Python trials install only the pinned dependency set; projects whose
  tests need the project itself installed (`pip install -e .`) should
  make the test command handle that, or run tests that import from the
  source tree.
- Node trials run `npm install` in the copy, which needs registry
  access (or a warm npm cache); the offline guarantee is Python-only.
- Assumes a single pass/fail boundary; flaky tests will mislead any
  bisection, this one included.

## Roadmap

See [ROADMAP.md](ROADMAP.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Development setup:

```sh
uv sync
uv run pytest
uv run ruff format --check . && uv run ruff check .
```

## License

MIT, see [LICENSE](LICENSE).

GitHub topics: `dependencies`, `debugging`, `developer-tools`,
`package-manager`, `bisect`.
