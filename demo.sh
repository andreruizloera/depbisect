#!/usr/bin/env bash
# End-to-end demo of depbisect against a deliberately broken dependency
# bump. Runs fully offline, in four parts:
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
#
# The scenario: a demo app pins brokenlib==1.0.0 and okpkg==1.0.0 (the
# committed, known-good state). Someone bumps both pins in the working
# tree (brokenlib -> 2.0.0, okpkg -> 1.1.0) and the tests start
# failing. depbisect figures out which package broke it, and at which
# release.
#
# Every line this script greps for is a line the README pastes. If the
# tool's output drifts from the docs, this exits nonzero and CI fails.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
WHEELS="$ROOT/examples/demo/wheels"
WORK="$(mktemp -d)"
PORT_FILE="$WORK/index-port"
SERVER_PID=""

cleanup() {
    if [ -n "$SERVER_PID" ]; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
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
"${PYTHON[@]}" "$ROOT/examples/demo/serve_index.py" "$PORT_FILE" &
SERVER_PID=$!
for _ in $(seq 1 80); do
    [ -s "$PORT_FILE" ] && break
    sleep 0.25
done
if [ ! -s "$PORT_FILE" ]; then
    echo "demo: local package index failed to start" >&2
    exit 1
fi
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
echo "demo: all four parts matched the output the README documents."
