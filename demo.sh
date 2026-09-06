#!/usr/bin/env bash
# End-to-end demo of depbisect against a deliberately broken dependency
# bump. Runs fully offline, in three parts:
#
#   1. Local wheels only: the "package index" is a directory of wheels
#      committed under examples/demo/wheels.
#   2. Candidate discovery from a real package index, served on
#      127.0.0.1 by examples/demo/serve_index.py, so the --online code
#      path (HTTP request, PEP 503 parsing) runs for real without the
#      network and without depending on anyone's release history.
#   3. A full --online bisection against that index, which contains one
#      release that cannot be installed anywhere: depbisect skips it and
#      says so, instead of blaming it for the regression.
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
    "1.0.0  (good)" \
    "2.0.0  (bad)" \
    "Bisection would need at most 2 test run(s)."

echo
echo "=============================================================="
echo "Part 3: the same bisection, candidates from the index"
echo "=============================================================="
echo "brokenlib 1.5.0 is published at this index as a Windows-only"
echo "CPython 3.8 wheel, so it cannot be installed here at all."
echo
echo "$ depbisect run --test \"python test_app.py\" --online"
"${DEPBISECT[@]}" run \
    --test "python test_app.py" \
    --online --index-url "$INDEX_URL" | tee "$WORK/part3.txt"
require "$WORK/part3.txt" \
    "bisect:   brokenlib==1.5.0 ... SKIP (will not install here)" \
    "Last passing:  1.2.0" \
    "First failing: 2.0.0" \
    "candidates from the package index" \
    "could not be installed here and was skipped rather than blamed: 1.5.0" \
    "the boundary above is therefore not tight."

# A release that would not install must never be reported as the culprit.
if grep -q "First failing: 1.5.0" "$WORK/part3.txt"; then
    echo "DEMO CHECK FAILED: an uninstallable release was blamed" >&2
    exit 1
fi

echo
echo "demo: all three parts matched the output the README documents."
