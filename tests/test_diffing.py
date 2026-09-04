"""Changed-dependency set computation."""

from depbisect.diffing import ChangedDep, diff_states
from depbisect.manifests import DepState


def state(pins: dict[str, str]) -> DepState:
    return DepState("python", "requirements.txt", pins=pins)


class TestDiff:
    def test_no_changes(self) -> None:
        assert diff_states(state({"a": "1.0"}), state({"a": "1.0"})) == []

    def test_version_change(self) -> None:
        changed = diff_states(state({"a": "1.0"}), state({"a": "2.0"}))
        assert changed == [ChangedDep("a", "1.0", "2.0")]
        assert changed[0].kind == "changed"
        assert changed[0].describe() == "1.0 -> 2.0"

    def test_added(self) -> None:
        (dep,) = diff_states(state({}), state({"new": "1.0"}))
        assert dep == ChangedDep("new", None, "1.0")
        assert dep.kind == "added"
        assert "absent" in dep.describe()

    def test_removed(self) -> None:
        (dep,) = diff_states(state({"old": "1.0"}), state({}))
        assert dep == ChangedDep("old", "1.0", None)
        assert dep.kind == "removed"
        assert "removed" in dep.describe()

    def test_mixed_sorted_by_name(self) -> None:
        good = state({"kept": "1.0", "bumped": "1.0", "dropped": "1.0"})
        bad = state({"kept": "1.0", "bumped": "2.0", "novel": "0.1"})
        changed = diff_states(good, bad)
        assert [d.name for d in changed] == ["bumped", "dropped", "novel"]

    def test_downgrade_counts_as_change(self) -> None:
        (dep,) = diff_states(state({"a": "2.0"}), state({"a": "1.0"}))
        assert dep.kind == "changed"
