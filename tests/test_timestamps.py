# tests/test_timestamps.py
import build


def test_known_apple_ns_to_utc():
    # Apple epoch is 2001-01-01 UTC. 0 ns -> that instant, in UTC.
    assert build.apple_ns_to_iso(0).startswith("2001-01-01T00:00:00")


def test_seconds_and_nanoseconds_agree():
    secs = 700_000_000           # seconds form
    ns = secs * 1_000_000_000    # nanoseconds form
    assert build.apple_ns_to_iso(secs) == build.apple_ns_to_iso(ns)


def test_garbage_returns_empty_not_raise():
    assert build.apple_ns_to_iso(None) == ""
    assert build.apple_ns_to_iso(10**30) == ""   # absurd -> "" not OverflowError
    assert build.apple_ns_to_iso("nope") == ""
