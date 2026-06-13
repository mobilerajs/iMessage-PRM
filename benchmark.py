#!/usr/bin/env python3
"""Benchmark a local MLX model against the agent's 130 hand-labels.

Measures how well the model reproduces my category labels, both on the full
6-way taxonomy and on the collapsed scheme we'd likely ship (Family / Service /
Personal). Reports accuracy, a confusion summary, and speed.

Usage: .venv/bin/python benchmark.py [model_id]
"""
import json, os, sys, time, glob
from collections import Counter, defaultdict
from mlx_lm import load, generate

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = sys.argv[1] if len(sys.argv) > 1 else "mlx-community/Qwen3-4B-Instruct-2507-4bit"

CATS = ["Family", "Friend", "Professional", "Service", "Acquaintance", "Wrong number"]
SYSTEM = (
    "You label a contact from a text-message summary into exactly ONE category. "
    "Reply with ONLY the category word, nothing else.\n\n"
    "Categories:\n"
    "- Family: spouse, parent, sibling, in-law, relative\n"
    "- Friend: personal/social friend\n"
    "- Professional: work, business, networking, investor, colleague, intro\n"
    "- Service: contractor, vendor, tradesperson, coach/instructor, delivery, "
    "marketplace buyer/seller, any one-off transactional contact\n"
    "- Acquaintance: loose or unclear personal tie\n"
    "- Wrong number: misdirected / not actually known"
)

def collapse(c):
    if c == "Family": return "Family"
    if c == "Service": return "Service"
    if c in ("Friend", "Professional", "Acquaintance"): return "Personal"
    return "Other"

def digest_text(d):
    lines = []
    for m in d["sample"]:
        who = "ME" if m["me"] else (m["from"][:12] if d["kind"] == "group" else "THEM")
        lines.append(f"  {who}: {(m['text'] or '')[:90]}")
    mem = (" members: " + ", ".join(d["members"][:6])) if d["kind"] == "group" else ""
    return (f"name: {d['name']} (in_contacts: {'yes' if d['in_contacts'] else 'no'}, "
            f"{'group' if d['kind']=='group' else '1:1'}){mem}\n"
            f"sent_by_me: {d['sent']}, received: {d['recv']}\n"
            f"messages:\n" + "\n".join(lines))

def parse_cat(out):
    o = out.strip().lower()
    for c in CATS:
        if c.lower() in o:
            return c
    return "?"

def main():
    digests = {d["key"]: d for d in json.load(open(os.path.join(HERE, "out/digests.json")))}
    labels = {}
    for p in glob.glob(os.path.join(HERE, "data/enrich_parts/part1.json")):
        labels.update(json.load(open(p)))
    items = [(k, v["category"]) for k, v in labels.items() if k in digests and v.get("category")]
    print(f"Model: {MODEL}\nBenchmarking {len(items)} labeled conversations...\n")

    t0 = time.time()
    model, tok = load(MODEL)
    print(f"loaded in {time.time()-t0:.1f}s\n")

    correct = correct_c = 0
    conf = defaultdict(Counter)   # collapsed truth -> predicted collapsed
    misses = []
    times = []
    # The -Instruct-2507 models are non-thinking already; the plain Qwen3 hybrid
    # models need thinking explicitly disabled for fast, direct classification.
    ct_kwargs = {} if "2507" in MODEL else {"enable_thinking": False}
    for i, (key, truth) in enumerate(items):
        prompt = tok.apply_chat_template(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": digest_text(digests[key]) + "\n\nCategory:"}],
            add_generation_prompt=True, tokenize=False, **ct_kwargs)
        t1 = time.time()
        out = generate(model, tok, prompt=prompt, max_tokens=8, verbose=False)
        times.append(time.time() - t1)
        pred = parse_cat(out)
        if pred == truth: correct += 1
        tc, pc = collapse(truth), collapse(pred)
        if tc == pc: correct_c += 1
        conf[tc][pc] += 1
        if tc != pc:
            misses.append((digests[key]["name"][:24], truth, pred))
        if (i + 1) % 30 == 0:
            print(f"  {i+1}/{len(items)}...")

    n = len(items)
    print(f"\n=== RESULTS: {MODEL} ===")
    print(f"Full 6-way accuracy:      {correct}/{n} = {correct/n*100:.1f}%")
    print(f"Collapsed (Fam/Svc/Pers): {correct_c}/{n} = {correct_c/n*100:.1f}%")
    print(f"Speed: {sum(times)/n*1000:.0f} ms/convo  (≈ {1028*sum(times)/n:.0f}s for all 1028)")
    print("\nCollapsed confusion (truth -> predicted):")
    for tc in ["Family", "Service", "Personal", "Other"]:
        if conf[tc]:
            print(f"  {tc:9}: " + ", ".join(f"{p}:{c}" for p, c in conf[tc].most_common()))
    print(f"\nCollapsed mismatches ({len(misses)}):")
    for name, tr, pr in misses[:40]:
        print(f"  {name:24} mine={tr:13} model={pr}")

if __name__ == "__main__":
    main()
