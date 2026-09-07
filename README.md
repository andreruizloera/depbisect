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

The demo runs fully offline and has three parts. The first breaks a
tiny app with a bad dependency bump and lets depbisect find the
culprit:

```text
$ cat requirements.txt
brokenlib==2.0.0
okpkg==1.1.0

$ depbisect run --test "python test_app.py" --no-index --find-links examples/demo/wheels
workspace: /var/folders/.../depbisect-1ampgdg2/project (your project is not touched)
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
  Searched:      4 version(s), candidates from local wheels
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

## Online mode: bisect every published release

Without `--online`, depbisect makes no network request of its own and
can only test versions it can find in a local wheel directory. If it
has none, "which release broke this" degrades to "somewhere between
the two versions in your manifest".

Here is a real regression: `from jinja2 import escape` was removed in
Jinja2 3.1.0, and the pins go 3.0.3 to 3.1.4. Offline, depbisect knows
the culprit package and nothing more:

```text
$ depbisect run --test "python test_app.py"
baseline: testing bad dependency state ... FAIL
baseline: testing good dependency state ... PASS

Dependency regression isolated

  Package:       jinja2
  Last passing:  3.0.3
  First failing: 3.1.4
  Runs:          2
  Searched:      2 version(s), no intermediate candidates

  note: no intermediate versions were available, so this bisected only between the two known versions. Releases between 3.0.3 and 3.1.4 were not tested.
        --online would look them up at the package index.
```

With `--online` it asks the index which releases exist in between, and
two more test runs turn a four-release window into the exact release:

```text
$ depbisect run --test "python test_app.py" --online
baseline: testing bad dependency state ... FAIL
baseline: testing good dependency state ... PASS
bisect:   jinja2==3.1.1 ... FAIL
bisect:   jinja2==3.1.0 ... FAIL

Dependency regression isolated

  Package:       jinja2
  Last passing:  3.0.3
  First failing: 3.1.0
  Runs:          4
  Searched:      6 version(s), candidates from the package index
```

`depbisect versions` answers "what would it actually walk?" without
installing or running anything:

```text
$ depbisect versions jinja2 --from 3.0.3 --to 3.1.4 --online
jinja2: 6 candidate version(s) to bisect (source: index https://pypi.org/simple/)

  3.0.3  (good)
  3.1.0
  3.1.1
  3.1.2
  3.1.3
  3.1.4  (bad)

  Bisection would need at most 3 test run(s).
```

### What the index list is filtered by, and why

An index lists every release a project ever published. Handing that
list to a bisection unfiltered spends test runs on releases that
cannot answer the question:

- **Pre-releases are excluded** unless you pass `--pre`. Reporting
  `2.0.0rc1` as the first failing release is not useful when nobody
  installed it.
- **Yanked releases are excluded** unless you pass `--include-yanked`.
- **Releases whose `Requires-Python` excludes your interpreter are
  excluded.** depbisect creates its trial virtualenv with the
  interpreter it is running under, so this is a fact about the run and
  not a guess. On Python 3.13, bisecting numpy 1.19.0 to 1.26.0 drops
  the five 1.21.x releases that declare `>=3.7,<3.11`.
- **Releases with no distribution that could install on this platform
  are excluded**, read out of the wheel filenames in the listing. See
  below.
- **Filtering never applies to `--find-links`.** A wheel you put in a
  directory is a version you chose; depbisect does not second-guess it.

Every count is reported, and it counts only releases inside the
good-to-bad interval, so "5 not compatible" is a fact about this
bisection rather than about the project's whole history.

### A release that cannot install here is excluded, or skipped, never blamed

Two different things hide behind "this release will not install", and
depbisect answers them differently.

The first one the index listing already settles. A wheel filename
states the interpreters, ABIs, and platforms the wheel was built for
(PEP 425), so a release whose every distribution is a wheel for some
other platform can be dropped before the search starts. It costs no
test run and it does not become a gap in the boundary:

```text
$ depbisect versions brokenlib --from 1.0.0 --to 2.0.0 --online
brokenlib: 5 candidate version(s) to bisect (source: index http://127.0.0.1:52554/simple/)

  1.0.0  (good)
  1.1.0
  1.2.0
  1.6.0
  2.0.0  (bad)

  Not bisected (1):
    1.5.0  no distribution for this platform
    (this platform: macosx_15_0_arm64, Python 3.13.14)

  Bisection would need at most 2 test run(s).
```

brokenlib 1.5.0 is published at that index as a Windows-only CPython
3.8 wheel. The tag set it is compared against is computed from the
interpreter depbisect is running under, which is the interpreter the
trial virtualenv is created with, so this is a fact about the run and
not a guess about the machine. `depbisect versions` names every release
it filtered out and why, since showing the candidate path is the entire
job of that command.

The second one no filename can settle. brokenlib 1.6.0 publishes only a
source distribution, and an sdist may well build here: "no compatible
wheel" and "not installable" are different claims, and only the first
is readable from a listing. So 1.6.0 stays a candidate and gets probed.
Its build fails, and that is a third answer rather than a test failure,
because otherwise the bisection would blame a release it never ran:

```text
$ depbisect run --test "python test_app.py" --online
baseline: testing bad dependency state ... FAIL
baseline: testing good dependency state ... PASS
subset:   reverting {brokenlib} ... PASS
bisect:   brokenlib==1.2.0 ... PASS
bisect:   brokenlib==1.6.0 ... SKIP (will not install here)

Dependency regression isolated

  Package:       brokenlib
  Last passing:  1.2.0
  First failing: 2.0.0
  Test:          python test_app.py
  Runs:          5
  Searched:      5 version(s), candidates from the package index

  note: 1 release between 1.2.0 and 2.0.0 could not be installed here and was skipped rather than blamed: 1.6.0
        the boundary above is therefore not tight.

  note: excluded from the index list: 1 with no distribution for this platform (macosx_15_0_arm64)
```

The search steps to the nearest usable neighbour instead, and any
skipped release still inside the final boundary is named, because it
is a gap in the evidence. That is part 3 of `./demo.sh`, which serves
the releases from a local package index so the whole thing runs in CI
without a network.

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

# Bisect every release published between the two pins:
depbisect run --test "pytest" --online

# Just show the candidate list:
depbisect versions requests --from 2.28.0 --to 2.31.0 --online
```

Options for `depbisect run`:

| Flag | Meaning |
| --- | --- |
| `--test CMD` | test command that fails now (required) |
| `--good-ref REF` | git ref of the known-good dependency state (default: auto-detect) |
| `--bad-ref REF` | git ref of the known-bad state (default: working tree) |
| `-C, --directory DIR` | project directory (default: `.`) |
| `--dry-run` | print changed deps, candidates, and a run estimate; mutate nothing |
| `--find-links DIR` | local wheel/sdist directory; install source and offline candidate list (repeatable) |
| `--online` | query the package index for candidate releases (Python only; off by default) |
| `--index-url URL` | simple-index base URL for `--online` and for installs (default `https://pypi.org/simple/`) |
| `--pre` | include pre-releases from the index |
| `--include-yanked` | include yanked releases from the index |
| `--no-index` | never touch the package index; install only from `--find-links` |
| `--timeout SECONDS` | per-command timeout (default 600) |
| `--keep-temp` | keep the temporary workspace for inspection |
| `--verbose` | stream install and test output |

`depbisect versions PKG --from A --to B` prints the candidate path for
one package and exits. It takes the same `--find-links`, `--online`,
`--index-url`, `--pre`, and `--include-yanked` flags, installs
nothing, and runs no tests.

Exit codes: 0 success, 2 usage or git error, 130 interrupted.

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

depbisect makes no network request of its own unless you pass
`--online`, and even then it only issues GET requests to the simple
index, over http or https and no other scheme. It reads the response
as text; nothing is executed and nothing is written to disk. Installs
are a separate matter: `pip` and `npm` reach the network by default as
they always have, and `--no-index --find-links DIR` is still the way
to forbid that.

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
                semver shapes), distribution-filename parsing, candidate
                discovery from local wheel directories
  specifiers.py Requires-Python evaluation (the PEP 440 operator subset
                that appears in real metadata), pure
  tags.py       PEP 425 compatibility tags: wheel-filename tag parsing
                and the tag set of the running interpreter, pure
  index.py      the only module that touches the network: simple
                repository API client, PEP 691 JSON and PEP 503 HTML
  candidates.py composes local and index candidates into the path a
                bisection walks, with an account of what was filtered
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

- **Index candidate discovery is Python only.** `--online` speaks the
  simple repository API; there is no npm registry client, so Node
  candidate versions still come from `--find-links` alone. Without
  either source depbisect bisects between the two known versions only,
  and the report says so.
- **A skipped release still leaves a real gap.** Wheel tags take the
  releases that could never have installed here out of the search
  before it starts, but they cannot speak for a source distribution: an
  sdist may build or may not, and depbisect only finds out by trying.
  When a candidate cannot be installed, depbisect reports the boundary
  it could reach and names what it could not test. That boundary is
  honest, but it is wider than a fully-tested one, and the breaking
  change may be inside the gap.
- **Wheel-tag filtering reads filenames, not wheels.** The supported
  tag set is computed from the running interpreter and compared with
  what each filename declares, so a release is excluded only when every
  distribution it published is a wheel tagged for another platform. Two
  deliberate holes stop that from deleting real candidates: an sdist,
  or a filename that does not parse, is always kept; and a host whose
  platform tags depbisect cannot work out (a musl system, mostly)
  excludes nothing at all and falls back to probing. Like
  `Requires-Python` filtering this has no override flag, and like every
  other filter it never touches `--find-links`.
- **`Requires-Python` filtering trusts the index's metadata.** A
  release whose declared support is wrong (too generous or too strict)
  is filtered on what it declared, not on what it does. A wrongly
  strict declaration silently removes a real candidate; `--pre` and
  `--include-yanked` have escape hatches, this one does not.
- **The candidate list is the index's, not a resolver's.** depbisect
  pins one package at a time and lets the installer resolve the rest,
  so a candidate can fail to install because of a transitive conflict
  rather than anything about that release. That reads as a skip.
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
