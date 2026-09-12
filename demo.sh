#!/usr/bin/env bash
# End-to-end demo of depbisect against a deliberately broken dependency
# bump. Runs fully offline, in five parts:
#
#   1. Local wheels only: the "package index" is a directory of wheels
#      committed under examples/demo/wheels.
#   2. Candidate discovery from a real package index, served on
#      127.0.0.1 by examples/demo/serve_index.py, so the --online code
#      path (HTTP request, PEP 503 parsing) runs for real without the
#      network and without depending on anyone's release history.
#   3. A full --online bisection against that index, which publishes two
#      releases that cannot be installed here and must be treated
#      differently: one has only a Windows wheel, which the index
#      listing already proves unusable, so it is excluded before any
#      test run; the other has only an sdist, which might have built,
#      so it is probed, and skipped and named when it does not.
#   4. The same regression in a Poetry project, to show that the
#      manifest a project happens to use does not change the answer.
#   5. A Node project bisected against an npm registry, served on
#      127.0.0.1 by examples/demo/serve_registry.py, with real npm
#      installs. The registry lists a deprecated release, which stays a
#      candidate, and a pre-release and an AIX-only release, which do
#      not. Needs npm on PATH.
#
# The scenario for parts 1 to 4: a demo app pins brokenlib==1.0.0 and
# okpkg==1.0.0 (the committed, known-good state). Someone bumps both pins
# in the working tree (brokenlib -> 2.0.0, okpkg -> 1.1.0) and the tests
# start failing. depbisect figures out which package broke it, and at
# which release. Part 5 is the same shape with padstr 1.0.0 -> 2.0.0.
#
# Every line this script greps for is a line the README pastes. If the
# tool's output drifts from the docs, this exits nonzero and CI fails.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
WHEELS="$ROOT/examples/demo/wheels"
WORK="$(mktemp -d)"
PORT_FILE="$WORK/index-port"
REGISTRY_PORT_FILE="$WORK/registry-port"
SERVER_PIDS=()

cleanup() {
    for pid in "${SERVER_PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    done
    rm -rf "$WORK"
}
trap cleanup EXIT

if command -v depbisect >/dev/null 2>&1; then
    DEPBISECT=(depbisect)
    PYTHON=(python3)
else
    DEPBISECT=(uv run --project "$ROOT" --quiet depbisect)
    PYTHON=(uv run --project "$ROOT" --quiet python)
fi

require() {
    # require <file> <string...>: every string must appear in the file.
    local file="$1"
    shift
    for needle in "$@"; do
        if ! grep -qF -- "$needle" "$file"; then
            echo "DEMO CHECK FAILED: expected to find: $needle" >&2
            echo "--- actual output ---" >&2
            cat "$file" >&2
            exit 1
        fi
    done
}

start_server() {
    # start_server <script> <portfile>: run a local server, wait for its port.
    "${PYTHON[@]}" "$1" "$2" &
    SERVER_PIDS+=("$!")
    for _ in $(seq 1 80); do
        [ -s "$2" ] && return 0
        sleep 0.25
    done
    echo "demo: $(basename "$1") failed to start" >&2
    exit 1
}

# Set up the demo project as a git repo with a known-good committed state.
PROJECT="$WORK/project"
mkdir -p "$PROJECT"
cp -R "$ROOT/examples/demo/project/." "$PROJECT"
cd "$PROJECT"
git init -q
git -c user.name=demo -c user.email=demo@example.invalid add .
git -c user.name=demo -c user.email=demo@example.invalid commit -qm "pin known-good dependencies"

# The bad dependency bump, uncommitted, exactly as you'd have it after
# noticing your tests broke.
printf 'brokenlib==2.0.0\nokpkg==1.1.0\n' > requirements.txt

echo "=============================================================="
echo "Part 1: offline, candidates from a local wheel directory"
echo "=============================================================="
echo "$ cat requirements.txt"
cat requirements.txt
echo
echo "$ depbisect run --test \"python test_app.py\" --no-index --find-links examples/demo/wheels"
"${DEPBISECT[@]}" run \
    --test "python test_app.py" \
    --no-index \
    --find-links "$WHEELS" | tee "$WORK/part1.txt"
require "$WORK/part1.txt" \
    "Package:       brokenlib" \
    "Last passing:  1.2.0" \
    "First failing: 2.0.0" \
    "candidates from local wheels"

# Start the local package index for parts 2 and 3.
start_server "$ROOT/examples/demo/serve_index.py" "$PORT_FILE"
INDEX_URL="http://127.0.0.1:$(cat "$PORT_FILE")/simple/"

echo
echo "=============================================================="
echo "Part 2: which releases would a bisection actually walk?"
echo "=============================================================="
echo "$ depbisect versions brokenlib --from 1.0.0 --to 2.0.0 --online"
"${DEPBISECT[@]}" versions brokenlib \
    --from 1.0.0 --to 2.0.0 \
    --online --index-url "$INDEX_URL" | tee "$WORK/part2.txt"
require "$WORK/part2.txt" \
    "5 candidate version(s) to bisect" \
    "source: index http://127.0.0.1:" \
    "1.0.0  (good)" \
    "1.1.0" \
    "1.2.0" \
    "1.6.0" \
    "2.0.0  (bad)" \
    "Not bisected (1):" \
    "1.5.0  no distribution for this platform" \
    "(this platform: " \
    "Bisection would need at most 2 test run(s)."

# The Windows-only release must not appear in the path itself.
if grep -qE "^  1\.5\.0$" "$WORK/part2.txt"; then
    echo "DEMO CHECK FAILED: a release with no usable distribution is still a candidate" >&2
    exit 1
fi

echo
echo "=============================================================="
echo "Part 3: the same bisection, candidates from the index"
echo "=============================================================="
echo "brokenlib 1.5.0 is published at this index as a Windows-only"
echo "CPython 3.8 wheel: its tags rule this host out, so it is excluded"
echo "from the candidate path and costs no test run. brokenlib 1.6.0 is"
echo "published only as an sdist, which might have built here, so it is"
echo "kept, probed, and skipped when the build fails."
echo
echo "$ depbisect run --test \"python test_app.py\" --online"
"${DEPBISECT[@]}" run \
    --test "python test_app.py" \
    --online --index-url "$INDEX_URL" | tee "$WORK/part3.txt"
require "$WORK/part3.txt" \
    "baseline: testing bad dependency state ... FAIL" \
    "baseline: testing good dependency state ... PASS" \
    "subset:   reverting {brokenlib} ... PASS" \
    "bisect:   brokenlib==1.2.0 ... PASS" \
    "bisect:   brokenlib==1.6.0 ... SKIP (will not install here)" \
    "Dependency regression isolated" \
    "Package:       brokenlib" \
    "Last passing:  1.2.0" \
    "First failing: 2.0.0" \
    "Test:          python test_app.py" \
    "Runs:          5" \
    "Searched:      5 version(s), candidates from the package index" \
    "could not be installed here and was skipped rather than blamed: 1.6.0" \
    "the boundary above is therefore not tight." \
    "note: excluded from the index list: 1 with no distribution for this platform"

# A release that would not install must never be reported as the culprit.
if grep -qE "First failing: 1\.(5|6)\.0" "$WORK/part3.txt"; then
    echo "DEMO CHECK FAILED: an uninstallable release was blamed" >&2
    exit 1
fi

# The whole point of the tag filter: no test run is spent on a release
# the index listing already ruled out.
if grep -q "bisect:   brokenlib==1.5.0" "$WORK/part3.txt"; then
    echo "DEMO CHECK FAILED: a test run was spent on a tag-excluded release" >&2
    exit 1
fi

echo
echo "=============================================================="
echo "Part 4: the same regression, pinned in a poetry.lock"
echo "=============================================================="
POETRY_PROJECT="$WORK/poetry-project"
mkdir -p "$POETRY_PROJECT"
cp "$ROOT/examples/demo/project/app.py" "$ROOT/examples/demo/project/test_app.py" "$POETRY_PROJECT"
cp "$ROOT/examples/demo/poetry/pyproject.toml" "$ROOT/examples/demo/poetry/poetry.lock" \
    "$POETRY_PROJECT"
cd "$POETRY_PROJECT"
git init -q
git -c user.name=demo -c user.email=demo@example.invalid add .
git -c user.name=demo -c user.email=demo@example.invalid commit -qm "lock known-good dependencies"
cp "$ROOT/examples/demo/poetry/bad/poetry.lock" "$POETRY_PROJECT/poetry.lock"

echo "$ depbisect run --test \"python test_app.py\" --find-links examples/demo/wheels --dry-run"
"${DEPBISECT[@]}" run \
    --test "python test_app.py" \
    --find-links "$WHEELS" \
    --dry-run | tee "$WORK/part4-plan.txt"
require "$WORK/part4-plan.txt" \
    "depbisect plan (dry run)" \
    "poetry-project (python)" \
    "Manifest:  poetry.lock" \
    "Good ref:  " \
    "Bad state: working tree" \
    "Test:      python test_app.py" \
    "Changed dependencies (2):" \
    "brokenlib  1.0.0 -> 2.0.0   (2 intermediate release(s) found locally)" \
    "okpkg      1.0.0 -> 1.1.0   (no intermediate versions found)" \
    "Estimated test runs: 4 to 14" \
    "Dry run: nothing was installed and no files were modified."

echo
echo "$ depbisect run --test \"python test_app.py\" --no-index --find-links examples/demo/wheels"
"${DEPBISECT[@]}" run \
    --test "python test_app.py" \
    --no-index \
    --find-links "$WHEELS" | tee "$WORK/part4.txt"
require "$WORK/part4.txt" \
    "Package:       brokenlib" \
    "Last passing:  1.2.0" \
    "First failing: 2.0.0" \
    "candidates from local wheels"

echo
echo "=============================================================="
echo "Part 5: a Node project, candidates from an npm registry"
echo "=============================================================="
if ! command -v npm >/dev/null 2>&1; then
    echo "demo: part 5 needs npm on PATH" >&2
    exit 1
fi
# Trials run real npm installs. Their cache and npm's update check are
# pointed away from the user's own, so the demo leaves nothing behind.
export npm_config_cache="$WORK/npm-cache"
export npm_config_update_notifier=false

start_server "$ROOT/examples/demo/serve_registry.py" "$REGISTRY_PORT_FILE"
REGISTRY_URL="http://127.0.0.1:$(cat "$REGISTRY_PORT_FILE")/"

NODE_PROJECT="$WORK/node-project"
mkdir -p "$NODE_PROJECT"
cp "$ROOT/examples/demo/node/package.json" "$ROOT/examples/demo/node/package-lock.json" \
    "$ROOT/examples/demo/node/test.js" "$NODE_PROJECT"
cd "$NODE_PROJECT"
git init -q
git -c user.name=demo -c user.email=demo@example.invalid add .
git -c user.name=demo -c user.email=demo@example.invalid commit -qm "pin padstr 1.0.0"
cp "$ROOT/examples/demo/node/bad/package.json" "$ROOT/examples/demo/node/bad/package-lock.json" \
    "$NODE_PROJECT"

echo "$ depbisect versions padstr --from 1.0.0 --to 2.0.0 --online --ecosystem node --index-url $REGISTRY_URL"
"${DEPBISECT[@]}" versions padstr \
    --from 1.0.0 --to 2.0.0 \
    --online --ecosystem node --index-url "$REGISTRY_URL" | tee "$WORK/part5-versions.txt"
require "$WORK/part5-versions.txt" \
    "padstr: 5 candidate version(s) to bisect (source: npm registry http://127.0.0.1:" \
    "1.0.0  (good)" \
    "1.1.0" \
    "1.2.0  (deprecated)" \
    "1.3.0" \
    "2.0.0  (bad)" \
    "Deprecated releases stay candidates: npm installs a release asked for by its exact version." \
    "Not bisected (2):" \
    "1.4.0-beta.1  pre-release (--pre to include)" \
    "1.5.0         not installable on this platform (os aix)" \
    "(this platform: " \
    "Bisection would need at most 2 test run(s)."

# Neither left-out release may be on the path itself.
if grep -qE "^  1\.(4\.0-beta\.1|5\.0)$" "$WORK/part5-versions.txt"; then
    echo "DEMO CHECK FAILED: a release the registry listing ruled out is still a candidate" >&2
    exit 1
fi

echo
echo "$ depbisect run --test \"node test.js\" --online --index-url $REGISTRY_URL"
"${DEPBISECT[@]}" run \
    --test "node test.js" \
    --online --index-url "$REGISTRY_URL" | tee "$WORK/part5.txt"
require "$WORK/part5.txt" \
    "baseline: testing bad dependency state ... FAIL" \
    "baseline: testing good dependency state ... PASS" \
    "bisect:   padstr@1.2.0 ... PASS" \
    "bisect:   padstr@1.3.0 ... FAIL" \
    "Dependency regression isolated" \
    "Package:       padstr" \
    "Last passing:  1.2.0" \
    "First failing: 1.3.0" \
    "Test:          node test.js" \
    "Runs:          4" \
    "Searched:      5 version(s), candidates from the npm registry" \
    "note: excluded from the registry list: 1 pre-release(s), 1 not installable on this platform ("

# No test run may be spent on a release the registry listing ruled out.
if grep -qE "bisect:   padstr@1\.(4\.0-beta\.1|5\.0) " "$WORK/part5.txt"; then
    echo "DEMO CHECK FAILED: a test run was spent on an excluded release" >&2
    exit 1
fi

echo
echo "demo: all five parts matched the output the README documents."
