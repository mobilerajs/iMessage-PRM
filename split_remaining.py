#!/usr/bin/env python3
"""Split not-yet-classified digests into compact chunk files for the workflow."""
import json, os, glob

HERE = os.path.dirname(os.path.abspath(__file__))
parts = glob.glob(os.path.join(HERE, "data/enrich_parts/*.json"))
done = set()
for p in parts:
    try:
        done |= set(json.load(open(p)).keys())
    except Exception:
        pass

digests = json.load(open(os.path.join(HERE, "out/digests.json")))
remaining = [d for d in digests if d["key"] not in done]

CHUNK = 55
outdir = os.path.join(HERE, "data/enrich_parts/_chunks")
os.makedirs(outdir, exist_ok=True)
for f in glob.glob(os.path.join(outdir, "*.txt")):
    os.remove(f)

def line(x):
    flag = ("C" if x["in_contacts"] else "-") + ("G" if x["kind"] == "group" else "p")
    samp = " // ".join(
        (("ME:" if m["me"] else (m["from"][:8] + ":") if x["kind"] == "group" else "TH:")
         + (m["text"] or "")[:75])
        for m in x["sample"])
    mem = (" mem=[" + ",".join(x["members"][:5]) + "]") if x["kind"] == "group" else ""
    return f'{x["key"]} |{flag}| {x["name"][:28]} | s/r={x["sent"]}/{x["recv"]}{mem} :: {samp}'

n = 0
for i in range(0, len(remaining), CHUNK):
    chunk = remaining[i:i + CHUNK]
    with open(os.path.join(outdir, f"chunk_{n:02d}.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(line(x) for x in chunk))
    n += 1

print(f"remaining={len(remaining)} chunks={n} dir={outdir}")
