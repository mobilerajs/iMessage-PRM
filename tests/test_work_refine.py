"""The Work-vs-Personal refinement applied to deterministic-Personal people.

`refine_personal(model_verdict, in_contacts) -> (category, unsure)` runs ONLY on
people whose deterministic base category is Personal. The in_contacts prior
(a resolved address-book name) is a strong PERSONAL signal; a bare unnamed number
leans transactional/Work. The `unsure` flag marks the guesses for the Phase-3
email lookup to later confirm.
"""

import pytest

from build import refine_personal


def test_verdict_work_is_confident_work():
    assert refine_personal("work", in_contacts=True) == ("Work", False)
    assert refine_personal("work", in_contacts=False) == ("Work", False)


def test_verdict_personal_is_confident_personal():
    assert refine_personal("personal", in_contacts=True) == ("Personal", False)
    assert refine_personal("personal", in_contacts=False) == ("Personal", False)


def test_unsure_named_contact_stays_personal_flagged():
    # Named (in_contacts) + unsure -> Personal, but flagged unsure.
    assert refine_personal("unsure", in_contacts=True) == ("Personal", True)


def test_unsure_bare_number_becomes_work_flagged():
    # Bare unnamed number + unsure -> Work, flagged unsure.
    assert refine_personal("unsure", in_contacts=False) == ("Work", True)


@pytest.mark.parametrize("verdict", ["work", "personal", "unsure"])
@pytest.mark.parametrize("named", [True, False])
def test_always_returns_valid_pair(verdict, named):
    cat, unsure = refine_personal(verdict, in_contacts=named)
    assert cat in {"Work", "Personal"}
    assert isinstance(unsure, bool)


# --- model reply parser (classify.py) --------------------------------------- #
from classify import _parse_work_personal


@pytest.mark.parametrize("raw,expected", [
    ("work", "work"),
    ("Work", "work"),
    ("work.", "work"),
    ("work\n", "work"),
    ("personal", "personal"),
    ("Personal — clearly social", "personal"),
    ("unsure", "unsure"),
    ("", "unsure"),          # empty -> unsure (no false work)
    ("maybe", "unsure"),     # unrecognized -> unsure, never silently work
    ("idk", "unsure"),
])
def test_parse_work_personal(raw, expected):
    assert _parse_work_personal(raw) == expected
