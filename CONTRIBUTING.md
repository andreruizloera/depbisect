# Contributing

Bug reports, parser fixes, and new ecosystem support are all welcome.

## Setup

```sh
git clone https://github.com/andreruizloera/depbisect
cd depbisect
uv sync
```

## Checks to run before a PR

```sh
uv run ruff format --check .
uv run ruff check .
uv run pytest
./demo.sh
```

All four must pass. CI runs the same commands.

## Ground rules

- The search core (`bisector.py`) stays pure: oracles in, results and
  run counts out. No subprocess or filesystem work in that module.
- The safety guarantee is non-negotiable: nothing may write to the
  user's project directory. `tests/test_sandbox.py` enforces it with
  byte-for-byte tree digests; new features need equivalent coverage.
- No new runtime dependencies without prior discussion; part of the
  point of the tool is that it installs instantly anywhere.
- Tests for algorithms use mocked oracles, never real installs. Real
  install tests must work offline: local wheels for Python, a registry
  served on 127.0.0.1 for Node.
- Adding an ecosystem means: a parser in `manifests.py` returning
  `{name: version}`, an installer branch in `sandbox.py`, and tests
  for both, plus honest README notes on any limitation.
