#!/usr/bin/env bash
# End-to-end demo of depbisect against a deliberately broken dependency
# bump. Runs fully offline: the "package index" is a directory of local
# wheels committed under examples/demo/wheels.
#
# The scenario: a demo app pins brokenlib==1.0.0 and okpkg==1.0.0 (the
# committed, known-good state). Someone bumps both pins in the working
# tree (brokenlib -> 2.0.0, okpkg -> 1.1.0) and the tests start
# failing. depbisect figures out which package broke it, and at which
# release.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
WHEELS="$ROOT/examples/demo/wheels"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if command -v depbisect >/dev/null 2>&1; then
    DEPBISECT=(depbisect)
else
    DEPBISECT=(uv run --project "$ROOT" --quiet depbisect)
fi

# Set up the demo project as a git repo with a known-good committed state.
cp -R "$ROOT/examples/demo/project/." "$WORK"
cd "$WORK"
git init -q
git -c user.name=demo -c user.email=demo@example.invalid add .
git -c user.name=demo -c user.email=demo@example.invalid commit -qm "pin known-good dependencies"

# The bad dependency bump, uncommitted, exactly as you'd have it after
# noticing your tests broke.
printf 'brokenlib==2.0.0\nokpkg==1.1.0\n' > requirements.txt

echo "$ cat requirements.txt"
cat requirements.txt
echo
echo "$ depbisect run --test \"python test_app.py\" --no-index --find-links examples/demo/wheels"
"${DEPBISECT[@]}" run \
    --test "python test_app.py" \
    --no-index \
    --find-links "$WHEELS"
