#!/usr/bin/env python3
"""Spike (Task 0.1): can local Qwen3-4B orchestrate a bounded tool-calling loop?

Goal: empirically decide whether the local model
`mlx-community/Qwen3-4B-Instruct-2507-4bit` (via mlx-lm) can reliably drive a
small (<=2 call) tool loop, OR whether we must fall back to
"server orchestrates / Qwen only judges".

This uses ONLY fake in-memory data. It does NOT touch chat.db, real email, or
Contacts. Loads the model the same way classify.py does (load/generate from
mlx_lm) and renders prompts with the tokenizer chat template, which natively
supports `tools=[...]` for Qwen's <tool_call> format.

Run:
    .venv/bin/python spikes/qwen_toolcall_spike.py
"""
import json
import os
import re
import sys

from mlx_lm import load, generate

MODEL = os.environ.get("CRM_MODEL", "mlx-community/Qwen3-4B-Instruct-2507-4bit")
N_RUNS = 10
MAX_TOOL_CALLS = 2          # hard cap on the loop
GEN_MAX_TOKENS = 256
CONTACT = "Alex Rivera"

# ---------------------------------------------------------------------------
# The single fake tool the model is given. Returns 3 realistic fake emails.
# Hardcoded — no network, no disk. `top_n` is honored so we can see whether the
# model passes sane args.
# ---------------------------------------------------------------------------
FAKE_EMAILS = [
    {
        "from": "alex.rivera@acmeworks.com",
        "subject": "Re: Q3 roadmap review — agenda + pre-read",
        "date": "2026-05-28",
        "snippet": "Thanks for sending the deck. Can we move the roadmap "
                   "review to Thursday 2pm? I'll loop in the platform team.",
    },
    {
        "from": "alex.rivera@acmeworks.com",
        "subject": "Signed MSA + SOW for the integration work",
        "date": "2026-05-12",
        "snippet": "Attaching the countersigned MSA and the SOW. Legal cleared "
                   "it this morning, so we're good to kick off the project.",
    },
    {
        "from": "alex.rivera@acmeworks.com",
        "subject": "Standup notes — sprint 14",
        "date": "2026-04-30",
        "snippet": "Quick recap from standup: API migration is on track, demo "
                   "to the client Friday. Ping me if you need anything before then.",
    },
]


def search_email(query, top_n=3):
    """Fake email search. Returns up to top_n hardcoded work emails."""
    try:
        n = int(top_n)
    except (TypeError, ValueError):
        n = 3
    n = max(1, min(n, len(FAKE_EMAILS)))
    return FAKE_EMAILS[:n]


TOOLS = [{
    "type": "function",
    "function": {
        "name": "search_email",
        "description": ("Search the user's email archive for messages "
                        "to/from a person. Use this to gather evidence about "
                        "whether a contact is work-related."),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Name, email, or keywords to search for.",
                },
                "top_n": {
                    "type": "integer",
                    "description": "Maximum number of emails to return (1-10).",
                },
            },
            "required": ["query"],
        },
    },
}]

SYSTEM = (
    "You decide whether a named contact is a WORK contact or a PERSONAL "
    "contact. You have a search_email tool. Call it AT MOST ONCE to gather "
    "evidence, then stop calling tools and give your verdict.\n"
    "When you have enough evidence, do NOT call the tool again. Instead reply "
    "in plain text with exactly one line of the form:\n"
    "VERDICT: work=yes   (if work-related)\n"
    "VERDICT: work=no    (if personal)\n"
    "Include a one-sentence reason after the verdict line."
)

# Qwen emits tool calls as <tool_call>{"name":..., "arguments":{...}}</tool_call>.
# IMPORTANT: mlx-lm stops generation on the chat-template stop token, which
# frequently truncates the trailing </tool_call> tag. So we anchor on the
# OPENING <tool_call> tag and then balance-scan the JSON object that follows —
# we do NOT require the closing tag.
TOOL_CALL_OPEN_RE = re.compile(r"<tool_call>\s*", re.S)
# Verdict line: tolerant of spacing/casing, e.g. "VERDICT: work=yes".
VERDICT_RE = re.compile(r"work\s*=\s*(yes|no)", re.I)


def _scan_json_object(text, start):
    """Return (obj_str, end_index) for a balanced {...} starting at `start`,
    or (None, start) if none. Brace-counting, string-aware."""
    if start >= len(text) or text[start] != "{":
        return None, start
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1], i + 1
    return None, start  # unbalanced (truncated) — give up on this one


def parse_tool_calls(text):
    """Return a list of parsed {name, arguments} dicts found in model output.

    Tolerant of a missing </tool_call> closing tag (mlx-lm truncation)."""
    calls = []
    pos = 0
    while True:
        m = TOOL_CALL_OPEN_RE.search(text, pos)
        if not m:
            break
        blob, end = _scan_json_object(text, m.end())
        pos = end if end > m.end() else m.end() + 1
        if not blob:
            continue
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "name" in obj:
            args = obj.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            calls.append({"name": obj["name"], "arguments": args or {}})
    return calls


def parse_verdict(text):
    """Return 'yes'/'no' if a clean work= verdict is present, else None."""
    m = VERDICT_RE.search(text)
    return m.group(1).lower() if m else None


def gen(model, tok, messages):
    prompt = tok.apply_chat_template(
        messages, tools=TOOLS, add_generation_prompt=True, tokenize=False)
    return generate(model, tok, prompt=prompt,
                    max_tokens=GEN_MAX_TOKENS, verbose=False)


def run_once(model, tok):
    """One bounded agent loop. Returns a record describing what happened."""
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user",
         "content": f'Decide if "{CONTACT}" is a work contact. '
                    f'Use the tool if helpful.'},
    ]
    tool_calls_made = 0
    emitted_tool_call = False
    transcript = []

    # Allow up to MAX_TOOL_CALLS tool rounds, plus a final no-tool generation.
    for _ in range(MAX_TOOL_CALLS + 1):
        out = gen(model, tok, messages)
        transcript.append(out)
        calls = parse_tool_calls(out)

        if calls and tool_calls_made < MAX_TOOL_CALLS:
            emitted_tool_call = True
            call = calls[0]  # honor only the first call per turn
            # Record the assistant turn (with the tool call) for the template.
            messages.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "type": "function",
                    "function": {"name": call["name"],
                                 "arguments": call["arguments"]},
                }],
            })
            if call["name"] == "search_email":
                result = search_email(
                    call["arguments"].get("query", CONTACT),
                    call["arguments"].get("top_n", 3))
            else:
                result = {"error": f"unknown tool {call['name']}"}
            messages.append({"role": "tool",
                             "content": json.dumps(result)})
            tool_calls_made += 1
            continue

        # No (further) tool call this turn → this output is the final verdict.
        verdict = parse_verdict(out)
        return {
            "emitted_tool_call": emitted_tool_call,
            "tool_calls_made": tool_calls_made,
            "stopped_after_one": tool_calls_made == 1,
            "verdict": verdict,
            "verdict_parsed": verdict is not None,
            "final_text": out.strip()[:400],
            "transcript": transcript,
        }

    # Hit the loop cap without ever producing a tool-free turn. Take a last,
    # forced verdict generation with no tools so the model must commit.
    messages.append({
        "role": "user",
        "content": "Stop searching. Reply now with exactly: VERDICT: work=yes "
                   "or VERDICT: work=no, then a one-sentence reason.",
    })
    out = gen(model, tok, messages)
    transcript.append(out)
    verdict = parse_verdict(out)
    return {
        "emitted_tool_call": emitted_tool_call,
        "tool_calls_made": tool_calls_made,
        "stopped_after_one": tool_calls_made == 1,
        "verdict": verdict,
        "verdict_parsed": verdict is not None,
        "final_text": out.strip()[:400],
        "transcript": transcript,
    }


def main():
    verbose = "--verbose" in sys.argv
    print(f"Loading {MODEL} ...", flush=True)
    model, tok = load(MODEL)
    print(f"Running {N_RUNS} bounded tool-loops (cap {MAX_TOOL_CALLS} calls) "
          f"on contact={CONTACT!r}\n", flush=True)

    records = []
    for i in range(N_RUNS):
        rec = run_once(model, tok)
        records.append(rec)
        print(f"run {i+1:2}: tool_call={rec['emitted_tool_call']!s:5} "
              f"calls={rec['tool_calls_made']} "
              f"stop_after_1={rec['stopped_after_one']!s:5} "
              f"verdict={rec['verdict']!s:4} "
              f"parsed={rec['verdict_parsed']!s:5}",
              flush=True)
        if verbose:
            print("    final:", rec["final_text"].replace("\n", " ")[:200])

    n = len(records)
    valid_tool = sum(r["emitted_tool_call"] for r in records)
    stop1 = sum(r["stopped_after_one"] for r in records)
    parsed = sum(r["verdict_parsed"] for r in records)
    yes = sum(1 for r in records if r["verdict"] == "yes")

    print("\n=== Reliability over", n, "runs ===")
    print(f"valid tool call emitted : {valid_tool}/{n} "
          f"= {valid_tool/n*100:.0f}%")
    print(f"stopped after 1 call    : {stop1}/{n} = {stop1/n*100:.0f}%")
    print(f"verdict parses cleanly  : {parsed}/{n} = {parsed/n*100:.0f}%")
    print(f"verdict = work:yes      : {yes}/{n} "
          f"(expected yes; Alex is work)")
    # Distribution of #calls, to surface over-calling vs under-calling.
    from collections import Counter
    dist = Counter(r["tool_calls_made"] for r in records)
    print("tool-calls distribution :",
          ", ".join(f"{k} calls: {v}" for k, v in sorted(dist.items())))


if __name__ == "__main__":
    main()
