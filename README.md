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

The demo runs fully offline and has five parts: local wheels, a local
package index, a full online bisection against it, a Poetry lockfile,
and a Node project against a local npm registry (that part needs npm on
PATH). The first breaks a tiny app with a bad dependency bump and lets
depbisect find the culprit:

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
workspace: /var/folders/.../depbisect-zomwk401/project (your project is not touched)
baseline: testing bad dependency state ... FAIL
baseline: testing good dependency state ... PASS

Dependency regression isolated

  Package:       jinja2
  Last passing:  3.0.3
  First failing: 3.1.4
  Test:          python test_app.py
  Runs:          2
  Searched:      2 version(s), no intermediate candidates

  note: no intermediate versions were available, so this bisected only between the two known versions. Releases between 3.0.3 and 3.1.4 were not tested.
        --online would look them up at the package index.
```

With `--online` it asks the index which releases exist in between, and
two more test runs turn a four-release window into the exact release:

```text
$ depbisect run --test "python test_app.py" --online
workspace: /var/folders/.../depbisect-n_d0r3fk/project (your project is not touched)
baseline: testing bad dependency state ... FAIL
baseline: testing good dependency state ... PASS
bisect:   jinja2==3.1.1 ... FAIL
bisect:   jinja2==3.1.0 ... FAIL

Dependency regression isolated

  Package:       jinja2
  Last passing:  3.0.3
  First failing: 3.1.0
  Test:          python test_app.py
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

## Node projects: bisect against the npm registry

For a Node project, `--online` reads the npm registry, using the
`package-lock.json` depbisect already diffs. Here is a real regression:
in chalk 5.0.0, `require("chalk").red` stops being a function (measured
on Node 24.10.0, where `typeof chalk.red` is `function` for 4.1.2 and
`undefined` for 5.0.0). The pins go 4.1.0 to 5.3.0 and the test is:

```js
const chalk = require("chalk");
if (typeof chalk.red !== "function") {
  console.error("chalk.red is not a function");
  process.exit(1);
}
```

```text
$ depbisect run --test "node test.js" --online
workspace: /var/folders/.../depbisect-mbfghyoz/project (your project is not touched)
baseline: testing bad dependency state ... FAIL
baseline: testing good dependency state ... PASS
subset:   reverting {ansi-styles, chalk, color-convert} ... PASS
subset:   reverting {ansi-styles} ... FAIL
subset:   reverting {chalk} ... PASS
bisect:   chalk@5.0.1 ... FAIL
bisect:   chalk@4.1.2 ... PASS
bisect:   chalk@5.0.0 ... FAIL

Dependency regression isolated

  Package:       chalk
  Last passing:  4.1.2
  First failing: 5.0.0
  Test:          node test.js
  Runs:          8
  Searched:      10 version(s), candidates from the npm registry
```

That ran against registry.npmjs.org with npm 11.6.0. Three of the eight
runs are the subset stage: a lockfile pins every transitive package,
and chalk 5.3.0 declares no dependencies while chalk 4.1.0 declares two
(ansi-styles and supports-color) that pull in three more. The plan
listed all five as removed next to chalk itself, and depbisect first
showed that chalk alone explains the failure.
Without `--online` the same run can only report the two pinned versions.

### What the registry list is filtered by, and why

The rules follow what `npm install` itself enforces, read in npm's own
source (npm-install-checks, arborist, npm-pick-manifest) rather than
assumed:

- **Pre-releases are excluded** unless you pass `--pre`, by semver's
  rule: any hyphen before build metadata. That differs from the Python
  rule on purpose, because `2.0.0-post.1` is a pre-release to npm and a
  post-release to PEP 440.
- **Releases whose `os`, `cpu` or `libc` lists rule this host out are
  excluded.** npm refuses to install a non-optional package like that
  (`EBADPLATFORM`), so no trial could ever use it. The host is described
  by asking the `node` on your PATH for `process.platform`,
  `process.arch` and, on Linux, the C library family, which are the
  values npm compares.
- **Deprecated releases are kept, and named.** npm installs a deprecated
  release asked for by its exact version, which is how trials ask, and
  deprecation is often an end-of-life notice across a whole release
  line: on 2026-09-12 the registry listed 38 of uuid's 55 releases as
  deprecated, every one with the message `uuid@10 and below is no longer
  supported`. Excluding them would delete most of a real bisection.
- **`engines` is not a filter.** npm only warns on an `engines.node`
  mismatch unless `engine-strict` is set, so such a release installs and
  is tested.
- `--include-yanked` is refused for a Node project, since npm has no
  yanked releases.

`depbisect versions` asks the registry when given `--ecosystem node`.
Part 5 of `./demo.sh` serves one package from a local registry whose
listing exercises each rule once, and runs real `npm install`s against
it:

```text
$ depbisect versions padstr --from 1.0.0 --to 2.0.0 --online --ecosystem node --index-url http://127.0.0.1:64323/
padstr: 5 candidate version(s) to bisect (source: npm registry http://127.0.0.1:64323/)

  1.0.0  (good)
  1.1.0
  1.2.0  (deprecated)
  1.3.0
  2.0.0  (bad)

  Deprecated releases stay candidates: npm installs a release asked for by its exact version.

  Not bisected (2):
    1.4.0-beta.1  pre-release (--pre to include)
    1.5.0         not installable on this platform (os aix)
    (this platform: darwin arm64)

  Bisection would need at most 2 test run(s).
```

The `depbisect run` that follows tests 1.2.0 and 1.3.0 and nothing else,
and reports 1.3.0 as the first failing release in four runs. With an
explicit `--index-url`, trial installs come from that registry too.

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

# Node projects (--online reads the npm registry):
depbisect run --test "npm test"
depbisect run --test "npm test" --online

# See the plan without installing or running anything:
depbisect run --test "pytest" --dry-run

# Bisect every release published between the two pins:
depbisect run --test "pytest" --online

# Just show the candidate list:
depbisect versions requests --from 2.28.0 --to 2.31.0 --online
depbisect versions chalk --from 4.1.0 --to 5.3.0 --online --ecosystem node
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
| `--online` | query the package index, or the npm registry for a Node project, for candidate releases (off by default) |
| `--index-url URL` | simple-index or npm registry base URL for `--online` and for installs (default `https://pypi.org/simple/`, or `https://registry.npmjs.org/` for a Node project, whose installs then use npm's own configuration) |
| `--pre` | include pre-releases from the index |
| `--include-yanked` | include yanked releases from the index (Python only) |
| `--no-index` | never touch the package index; install only from `--find-links` |
| `--timeout SECONDS` | per-command timeout (default 600) |
| `--keep-temp` | keep the temporary workspace for inspection |
| `--verbose` | stream install and test output |

`depbisect versions PKG --from A --to B` prints the candidate path for
one package and exits. It takes the same `--find-links`, `--online`,
`--index-url`, `--pre`, and `--include-yanked` flags, installs
nothing, and runs no tests. Pass `--ecosystem node` for an npm package;
without it, `PKG` is looked up at the Python index.

Exit codes: 0 success, 2 usage or git error, 130 interrupted.

Supported manifests: `uv.lock`, `poetry.lock`, `Pipfile.lock`,
`requirements.txt` (pinned), pinned `pyproject.toml` dependencies for
Python; `package.json` plus `package-lock.json` (v1 to v3) for Node.
Lockfiles win when several are present, and which one a project uses
does not change the search:

```text
$ depbisect run --test "python test_app.py" --find-links examples/demo/wheels --dry-run
depbisect plan (dry run)

  Project:   /var/folders/.../poetry-project (python)
  Manifest:  poetry.lock
  Good ref:  bd50e2ab53461db0c62140aa1da616a50762f9c6 (bd50e2a)
  Bad state: working tree
  Test:      python test_app.py

  Changed dependencies (2):
    brokenlib  1.0.0 -> 2.0.0   (2 intermediate release(s) found locally)
    okpkg      1.0.0 -> 1.1.0   (no intermediate versions found)

  Estimated test runs: 4 to 14
  Dry run: nothing was installed and no files were modified.
```

Trials install the pinned set with `uv` or `pip` regardless of which
tool wrote the lockfile, so a Poetry or Pipenv project is bisected
without invoking `poetry` or `pipenv`. The plan above is part 4 of
`./demo.sh`, where the commit hash and the temporary path differ every
run.

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
index or the npm registry, over http or https and no other scheme. It
reads the response as text; nothing is executed and nothing is written
to disk. For a Node project it also runs `node -e` once, with a fixed
script, to learn the platform npm will check. Installs are a separate
matter: `pip` and `npm` reach the network by default as they always
have. `--no-index --find-links DIR` is the way to forbid that for a
Python project; a Node project has no equivalent short of an
`--index-url` registry you run yourself.

## Architecture

```
src/depbisect/
  cli.py        argument parsing and session orchestration
  manifests.py  lockfile/manifest parsers (uv.lock, poetry.lock,
                Pipfile.lock, requirements.txt, pyproject.toml,
                package-lock.json) -> {name: version}
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
  index.py      simple repository API client, PEP 691 JSON and PEP 503
                HTML; one of the two modules that touch the network
  npm.py        npm registry client (abbreviated package documents),
                npm's os/cpu/libc platform rule, and the Node host probe
  candidates.py composes local, index and registry candidates into the
                path a bisection walks, with an account of what was
                filtered
  sandbox.py    the safety layer: temp-copy workspace, fresh venvs,
                trial installs, test execution
```

The search core is pure: it sees only an oracle callable and returns a
result plus run counts, so the algorithm tests use mocked runners and
never install anything.

A session is: two baseline runs (bad state must fail, good state must
pass), then subset minimization, then version bisection of the single
culprit over the candidates found in local wheels and, with `--online`,
at the index or registry.

## Limitations

- **`--find-links` supplies no Node candidates.** An `npm pack` tarball
  (`name-version.tgz`) is not read as a distribution filename, and Node
  trials never point npm at the directory, so for a Node project the
  only source of intermediate releases is `--online`. Without it
  depbisect bisects between the two known versions only, and the report
  says so.
- **Without `--index-url`, a Node project's candidates and installs can
  come from different registries.** Candidates are read from
  registry.npmjs.org, while `npm install` uses whatever registry npm is
  configured with (`.npmrc`). A candidate the configured registry does
  not have fails to install and reads as a skip.
- **`depbisect versions` cannot tell which ecosystem a name belongs to.**
  Without `--ecosystem node` it asks the Python index, where an
  unrelated project with the same name can answer.
- **In a Node lockfile every transitive package is a pin.** A bump that
  changes a package's own dependencies shows up as several changed
  entries, and the subset stage spends runs ruling them out before the
  version bisection starts.
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
- Python trials install only the pinned dependency set, with `uv` or
  `pip`, whichever lockfile the pins came from. Projects whose tests
  need the project itself installed (`pip install -e .`) should make
  the test command handle that, or run tests that import from the
  source tree, and a project relying on Poetry or Pipenv install
  semantics beyond the pinned versions (extras resolution, group
  selection) gets the pins and not those semantics.
- Node trials run `npm install` in the copy, which needs a registry:
  the public one, npm's configured one, or one you serve yourself and
  name with `--index-url`, as part 5 of the demo does.
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
