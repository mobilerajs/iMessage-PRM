"""merge_recency: a person's recency is the later of their 1:1 thread and their
own activity in a shared group, so someone you chat with daily in a group isn't
flagged 'lost touch' just because the 1:1 thread is old."""
import build


def test_recent_group_beats_old_one_to_one():
    # Last 1:1 was 2019; still active in a group in 2026 -> recent.
    assert build.merge_recency("2019-01-01T00:00:00", "2026-06-01T12:00:00") == \
        "2026-06-01T12:00:00"


def test_recent_one_to_one_beats_old_group():
    assert build.merge_recency("2026-06-01T12:00:00", "2020-01-01T00:00:00") == \
        "2026-06-01T12:00:00"


def test_no_group_activity_falls_back_to_one_to_one():
    assert build.merge_recency("2024-03-02T09:00:00", "") == "2024-03-02T09:00:00"


def test_no_one_to_one_uses_group():
    assert build.merge_recency("", "2025-05-05T05:00:00") == "2025-05-05T05:00:00"


def test_both_empty():
    assert build.merge_recency("", "") == ""
