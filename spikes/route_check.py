#!/usr/bin/env python3
"""Route-check: prove identity/relationship descriptions don't become broad
computed rules.

The bug: the filter ROUTER turned an identity description ("friends of Sam")
into a degenerate computed rule {kind:person, in_contacts:true, two_way:true}
that matched ~450 people. It should route to SEMANTIC so the model reads each
conversation per-person.

The fix lives in classify.sanitize_route (the deterministic validation layer the
model's JSON proposal passes through). We assert directly on it — no model load
needed, so this runs in milliseconds.

  .venv/bin/python spikes/route_check.py
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from classify import sanitize_route, _has_discriminator

KIDS = ("These are friends of Sam since Sam doesn't have a phone so they "
        "sometimes message")

FAILURES = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got {got!r}")
    if not ok:
        FAILURES.append(f"{name}: expected {want!r}, got {got!r}")


def route_type(rule, desc, ftype="computed", **extra):
    d = {"type": ftype, "rule": rule, **extra}
    if ftype == "keyword":
        d = {"type": "keyword", "keywords": rule}
    return sanitize_route(d, desc).get("type")


print("route_check — shape-only computed rules must fall through to semantic\n")

print("-- THE BUG: identity/relationship descriptions route semantic --")
# The exact mis-route the model produced for Kids Friends.
check("kids-friends shape-only rule -> semantic",
      route_type({"kind": "person", "in_contacts": True, "two_way": True}, KIDS),
      "semantic")
check("in_contacts only -> semantic",
      route_type({"in_contacts": True}, "people I know"),
      "semantic")
check("two_way+counts+recency only -> semantic",
      route_type({"two_way": True, "min_count": 10, "min_days_since": 90},
                 "close people I lost touch with"),
      "semantic")

print("\n-- legit computed rules (real discriminator) still pass --")
check("category=Family stays computed",
      route_type({"kind": "person", "category": "Family"}, "my family"),
      "computed")
check("not_category=Family stays computed",
      route_type({"two_way": True, "not_category": "Family"}, "catch up but exclude family"),
      "computed")
check("group chats stay computed",
      route_type({"kind": "group"}, "my group chats"),
      "computed")
check("has_birthday stays computed",
      route_type({"has_birthday": True}, "people with a birthday"),
      "computed")

print("\n-- keyword type unaffected --")
check("keyword route preserved",
      route_type(["obo", "still available"], "marketplace", ftype="keyword"),
      "keyword")

print("\n-- _has_discriminator unit checks --")
assert _has_discriminator({"category": "Family"})
assert _has_discriminator({"not_category": "Family"})
assert _has_discriminator({"kind": "group"})
assert _has_discriminator({"has_birthday": True})
assert _has_discriminator({"has_suggestion": True})
assert not _has_discriminator({"in_contacts": True, "two_way": True})
assert not _has_discriminator({"kind": "person", "min_count": 5})
assert not _has_discriminator({"not_in_contacts": True, "max_days_since": 30})
print("  [PASS] discriminator helper")

if FAILURES:
    print("\nFAILURES:")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("\nAll route checks passed. Kids-Friends routes SEMANTIC, not a shape-only computed rule.")
