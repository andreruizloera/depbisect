"""Requires-Python evaluation. Pure, offline, no index involved."""

import pytest

from depbisect.specifiers import python_supported

PY313 = (3, 13, 1)
PY38 = (3, 8, 10)


class TestNoConstraint:
    @pytest.mark.parametrize("spec", [None, "", "   "])
    def test_absent_constraint_supports_everything(self, spec) -> None:
        assert python_supported(spec, PY313)


class TestSingleClause:
    @pytest.mark.parametrize(
        ("spec", "python", "expected"),
        [
            (">=3.8", PY313, True),
            (">=3.8", PY38, True),
            (">=3.9", PY38, False),
            (">3.13", PY313, True),  # 3.13.1 really is greater than 3.13
            (">3.13.1", PY313, False),
            ("<3.11", PY313, False),
            ("<3.11", PY38, True),
            ("<=3.8", PY38, False),  # 3.8.10 is past 3.8
            ("<=3.8.10", PY38, True),
            ("!=3.13", PY313, True),  # excludes 3.13 exactly, not 3.13.1
            ("!=3.13.1", PY313, False),
            ("!=3.12", PY313, True),
            ("==3.13.1", PY313, True),
        ],
    )
    def test_operators(self, spec: str, python: tuple[int, ...], expected: bool) -> None:
        assert python_supported(spec, python) is expected


class TestMultipleClauses:
    def test_every_clause_must_hold(self) -> None:
        # numpy 1.21.x really ships this one, and it is why those releases
        # must not be handed to a bisection running on 3.11 or newer.
        assert python_supported(">=3.7,<3.11", PY38)
        assert not python_supported(">=3.7,<3.11", PY313)

    def test_whitespace_between_clauses(self) -> None:
        assert python_supported(">=3.8, <4", PY313)
        assert not python_supported(">=3.8, <3.10", PY313)


class TestWildcards:
    @pytest.mark.parametrize(
        ("spec", "python", "expected"),
        [
            ("==3.13.*", PY313, True),
            ("==3.12.*", PY313, False),
            ("==3.*", PY313, True),
            ("!=3.13.*", PY313, False),
            ("!=3.8.*", PY313, True),
        ],
    )
    def test_prefix_match(self, spec: str, python: tuple[int, ...], expected: bool) -> None:
        assert python_supported(spec, python) is expected


class TestCompatibleRelease:
    def test_tilde_equals_pins_the_last_stated_segment(self) -> None:
        # ~=3.8 means >=3.8 within the 3.x series.
        assert python_supported("~=3.8", PY313)
        assert not python_supported("~=3.8", (2, 7, 18))
        assert not python_supported("~=3.8", (3, 7, 0))

    def test_tilde_equals_with_three_segments(self) -> None:
        # ~=3.8.1 means >=3.8.1 within 3.8.
        assert python_supported("~=3.8.1", (3, 8, 5))
        assert not python_supported("~=3.8.1", (3, 8, 0))
        assert not python_supported("~=3.9.0", (3, 13, 1))

    def test_tilde_equals_needs_two_segments(self) -> None:
        # "~=3" is not a legal compatible-release clause. Unreadable, so
        # it must not silently exclude the release.
        assert python_supported("~=3", PY313)


class TestArbitraryEquality:
    def test_triple_equals_is_a_string_match(self) -> None:
        assert python_supported("===3.13.1", PY313)
        assert not python_supported("===3.13", PY313)


class TestUnknownIsNotNo:
    """The safety rule: anything unreadable keeps the release in the list.

    Dropping a candidate is the destructive move, because the release
    then never gets tested and the bisection quietly reports a wider
    boundary. Keeping it costs at most one trial, which the install step
    resolves either way.
    """

    @pytest.mark.parametrize(
        "spec",
        [
            "not a specifier",
            ">=",
            "3.8",  # no operator
            ">= 3.8 or 3.9",
            "~=cheese",
        ],
    )
    def test_unparseable_specifier_supports_everything(self, spec: str) -> None:
        assert python_supported(spec, PY313)

    def test_an_unreadable_clause_does_not_veto_a_readable_one(self) -> None:
        # An unreadable clause contributes nothing; it does not turn the
        # whole specifier into a pass. Otherwise the verdict would depend
        # on the order the clauses were written in.
        assert not python_supported(">=3.99,garbage here", PY313)
        assert not python_supported("garbage here,>=3.99", PY313)

    def test_an_unreadable_clause_does_not_exclude_on_its_own(self) -> None:
        assert python_supported("garbage here,>=3.8", PY313)

    def test_a_readable_clause_still_excludes(self) -> None:
        assert not python_supported(">=3.99", PY313)
