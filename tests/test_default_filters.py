from build import DEFAULT_FILTERS, backfill_defaults


def test_default_filters_are_the_four_computed_categories():
    # The chips are now the four mutually-exclusive computed category filters.
    assert [f["id"] for f in DEFAULT_FILTERS] == [
        "family", "personal", "work", "contractors"]
    for f in DEFAULT_FILTERS:
        assert f["type"] == "computed"
        assert f["rule"]["kind"] == "person"
    # Retired chips are gone from the defaults.
    ids = {f["id"] for f in DEFAULT_FILTERS}
    for gone in ("service", "catchup", "catch-up", "groups", "junk"):
        assert gone not in ids


def test_seeded_defaults_added_when_absent():
    out_ids = {f["id"] for f in backfill_defaults([])}
    assert out_ids == {"family", "personal", "work", "contractors"}


def test_renamed_filter_suppresses_seeded_duplicate():
    # An existing 'contractors' (any equivalent slug) suppresses the seeded one.
    existing = [{"id": "contractors", "name": "Contractors", "type": "semantic"}]
    out = backfill_defaults(existing)
    assert sum(1 for f in out if f["id"] == "contractors") == 1
