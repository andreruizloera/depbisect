"""Read manifest content at git refs without ever touching the worktree.

Everything goes through ``git show`` and ``git log``; depbisect never
checks anything out over the user's files.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from depbisect.errors import DepbisectError

WORKTREE = "WORKTREE"  # sentinel ref meaning "the files on disk right now"


def _git(project: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(project), *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise DepbisectError(
            f"git {' '.join(args)} failed: {result.stderr.strip() or 'unknown error'}"
        )
    return result.stdout


def is_git_repo(project: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(project), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def read_at_ref(project: Path, ref: str, relpath: str) -> str:
    """Content of ``relpath`` at ``ref`` (or the worktree), via git show."""
    if ref == WORKTREE:
        path = project / relpath
        if not path.is_file():
            raise DepbisectError(f"{relpath} not found in {project}")
        return path.read_text()
    prefix = _git(project, "rev-parse", "--show-prefix").strip()
    return _git(project, "show", f"{ref}:{prefix}{relpath}")


def guess_good_ref(project: Path, relpath: str) -> str | None:
    """Newest commit whose copy of ``relpath`` differs from the worktree.

    This is the zero-config default: if you just bumped your lockfile
    (committed or not), the last state that differs is the natural
    known-good candidate. Returns None when no differing commit exists.
    """
    try:
        current = (project / relpath).read_text()
    except OSError:
        return None
    try:
        log = _git(project, "log", "--format=%H", "-n", "100", "--", relpath)
    except DepbisectError:
        return None
    for commit in log.split():
        try:
            content = read_at_ref(project, commit, relpath)
        except DepbisectError:
            continue
        if content != current:
            return commit
    return None


def short_ref(project: Path, ref: str) -> str:
    """Human-friendly form of a ref for reporting."""
    if ref == WORKTREE:
        return "working tree"
    try:
        out = _git(project, "rev-parse", "--short", ref).strip()
        return f"{ref} ({out})" if out != ref else out
    except DepbisectError:
        return ref
