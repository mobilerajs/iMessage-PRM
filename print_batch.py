#!/usr/bin/env python3
"""Print a compact slice of digests for agent classification.
Usage: python3 print_batch.py START COUNT
"""
import json, sys

d = json.load(open("out/digests.json"))
start = int(sys.argv[1]); count = int(sys.argv[2])
for x in d[start:start + count]:
    flag = ("C" if x["in_contacts"] else "-") + ("G" if x["kind"] == "group" else "p")
    samp = " // ".join(
        (("ME:" if m["me"] else (m["from"][:8] + ":") if x["kind"] == "group" else "TH:")
         + (m["text"] or "")[:70])
        for m in x["sample"]
    )
    mem = (" mem=[" + ",".join(x["members"][:5]) + "]") if x["kind"] == "group" else ""
    print(f'{x["key"]} |{flag}| {x["name"][:28]} | s/r={x["sent"]}/{x["recv"]}{mem} :: {samp}')
