"""The mutually-exclusive category partition.

Every person gets exactly ONE category from {Contractors, Family, Work, Personal}
assigned by PRIORITY (highest wins): Contractors -> Family -> Work -> Personal.

`base_category` is whatever the model/old logic produced (Family/Personal/Service);
a base of "Service" folds into Contractors.
"""

from build import assign_category

CONTRACTOR_KEYS = {"p100", "pboth"}
FAMILY_KEYS = {"p200"}
WORK_KEYS = {"p300", "pboth"}


def _assign(key, base=None):
    return assign_category(
        {"key": key},
        contractor_keys=CONTRACTOR_KEYS,
        family_keys=FAMILY_KEYS,
        work_keys=WORK_KEYS,
        base_category=base,
    )


def test_service_base_becomes_contractors():
    # A person the old logic called "Service" -> Contractors, even with no key match.
    assert _assign("p999", base="Service") == "Contractors"


def test_contractor_key_becomes_contractors():
    assert _assign("p100", base="Personal") == "Contractors"


def test_family_key_becomes_family():
    # In family_keys, not a contractor -> Family (even if base says Personal).
    assert _assign("p200", base="Personal") == "Family"


def test_work_key_becomes_work():
    # In work_keys only -> Work.
    assert _assign("p300", base="Personal") == "Work"


def test_plain_person_becomes_personal():
    assert _assign("p777", base="Personal") == "Personal"
    assert _assign("p777", base=None) == "Personal"


def test_priority_contractor_over_work():
    # pboth is in BOTH contractor_keys and work_keys -> Contractors wins.
    assert _assign("pboth", base="Personal") == "Contractors"


def test_priority_family_over_work():
    # A person in both family_keys and work_keys -> Family wins over Work.
    res = assign_category(
        {"key": "p200"},
        contractor_keys=set(),
        family_keys={"p200"},
        work_keys={"p200"},
        base_category="Personal",
    )
    assert res == "Family"


def test_always_one_of_four():
    for base in (None, "Family", "Personal", "Service"):
        assert _assign("pX", base=base) in {"Contractors", "Family", "Work", "Personal"}
