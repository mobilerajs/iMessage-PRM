"""Conservative model family detection (post-pass after the partition).

Family outranks Work and Personal (priority Contractors > Family > Work >
Personal). After the deterministic partition + work/personal refinement, the
model re-judges people currently Work or Personal: a `family` verdict promotes
them to Family. Contractors and already-Family people are never re-judged.
"""

import pytest

from build import apply_family_verdict, refine_family


# --- pure decision: should this person become Family? ----------------------- #
@pytest.mark.parametrize("category", ["Work", "Personal"])
def test_family_verdict_promotes_work_or_personal(category):
    assert apply_family_verdict(category, "family") == "Family"


@pytest.mark.parametrize("category", ["Work", "Personal"])
def test_not_family_leaves_category_unchanged(category):
    assert apply_family_verdict(category, "not_family") == category


@pytest.mark.parametrize("category", ["Contractors", "Family"])
def test_contractors_and_family_are_never_changed(category):
    # Even a 'family' verdict must not move a Contractor or re-touch Family.
    assert apply_family_verdict(category, "family") == category
    assert apply_family_verdict(category, "not_family") == category


# --- the post-pass over a people list --------------------------------------- #
def _people():
    # Promotable people must be saved contacts (in_contacts) — a real 1:1 relative
    # essentially always is, and refine_family() requires it.
    return [
        {"key": "p1", "kind": "person", "category": "Personal", "in_contacts": True},
        {"key": "p2", "kind": "person", "category": "Work", "in_contacts": True},
        {"key": "p3", "kind": "person", "category": "Personal", "in_contacts": True},
        {"key": "p4", "kind": "person", "category": "Contractors", "in_contacts": True},
        {"key": "p5", "kind": "person", "category": "Family", "in_contacts": True},
        {"key": "g1", "kind": "group", "category": "Personal", "in_contacts": False},
    ]


def test_refine_family_promotes_only_eligible():
    people = _people()
    verdicts = {
        "p1": "family",       # Personal -> Family
        "p2": "family",       # Work -> Family
        "p3": "not_family",   # stays Personal
        "p4": "family",       # Contractor: never judged, must stay Contractors
        "p5": "family",       # already Family: untouched
    }
    n = refine_family(people, verdicts)
    by_key = {p["key"]: p["category"] for p in people}
    assert by_key["p1"] == "Family"
    assert by_key["p2"] == "Family"
    assert by_key["p3"] == "Personal"
    assert by_key["p4"] == "Contractors"
    assert by_key["p5"] == "Family"
    assert n == 2  # only p1, p2 newly became Family


def test_refine_family_with_no_verdicts_changes_nothing():
    people = _people()
    before = [p["category"] for p in people]
    n = refine_family(people, {})
    after = [p["category"] for p in people]
    assert before == after
    assert n == 0


def test_bare_non_contact_is_not_promoted():
    # A 'family' verdict on an unsaved number (marketplace stranger that merely
    # says "my mom") must NOT promote — real 1:1 family is a saved contact.
    people = [{"key": "p9", "kind": "person", "category": "Personal",
               "in_contacts": False, "name": "+15550000000"}]
    n = refine_family(people, {"p9": "family"})
    assert people[0]["category"] == "Personal"
    assert n == 0


def test_school_parent_name_is_vetoed():
    # "Pat (Riley's Dad)" is someone else's parent, not the user's family.
    people = [{"key": "p10", "kind": "person", "category": "Personal",
               "in_contacts": True, "name": "Pat (Riley's Dad)"}]
    n = refine_family(people, {"p10": "family"})
    assert people[0]["category"] == "Personal"
    assert n == 0


def test_user_self_is_vetoed():
    people = [{"key": "p11", "kind": "person", "category": "Work",
               "in_contacts": True, "name": "Sam Taylor"}]
    n = refine_family(people, {"p11": "family"}, user_name="Sam Taylor")
    assert people[0]["category"] == "Work"
    assert n == 0
