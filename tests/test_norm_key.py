# tests/test_norm_key.py
import build


def test_us_formats_still_collapse_to_last10():
    assert build.norm_key("+1 (555) 123-4567") == build.norm_key("5551234567")
    assert build.norm_key("+15551234567") == "5551234567"


def test_distinct_intl_numbers_do_not_collide():
    # Same last 10 digits, different country codes -> different keys now.
    uk = build.norm_key("+44 20 5551234567"[:])
    other = build.norm_key("+33 1 5551234567")
    assert uk != other


def test_email_unchanged():
    assert build.norm_key("A@B.com ") == "a@b.com"
