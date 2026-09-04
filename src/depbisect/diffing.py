"""Compute the changed-dependency set between two manifest states."""

from __future__ import annotations

from dataclasses import dataclass

from depbisect.manifests import DepState


@dataclass(frozen=True)
class ChangedDep:
    """One dependency that differs between the good and bad states.

    ``good`` is None when the package was added in the bad state;
    ``bad`` is None when it was removed.
    """

    name: str
    good: str | None
    bad: str | None

    @property
    def kind(self) -> str:
        if self.good is None:
            return "added"
        if self.bad is None:
            return "removed"
        return "changed"

    def describe(self) -> str:
        if self.kind == "added":
            return f"(absent) -> {self.bad}"
        if self.kind == "removed":
            return f"{self.good} -> (removed)"
        return f"{self.good} -> {self.bad}"


def diff_states(good: DepState, bad: DepState) -> list[ChangedDep]:
    """All dependencies whose pinned version differs between two states."""
    changed: list[ChangedDep] = []
    for name in sorted(set(good.pins) | set(bad.pins)):
        gv, bv = good.pins.get(name), bad.pins.get(name)
        if gv != bv:
            changed.append(ChangedDep(name, gv, bv))
    return changed
