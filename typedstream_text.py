"""
Decode the plain text out of an iMessage `attributedBody` blob.

Modern macOS (and this database) stores message text almost exclusively in the
`attributedBody` column as an Apple "typedstream" (NSArchiver) blob rather than
the plain `text` column. The main message string is archived as an NSString.

This is a deliberately small, defensive extractor: it locates the NSString
payload and reads its length-prefixed UTF-8 bytes. It does not attempt to parse
the full typedstream object graph (attachments, run attributes, mentions); it
just recovers the human-readable message body, which is what the CRM needs.

Format around the string (observed):
    ... NSString \x01 \x94 \x84 \x01 <len> <utf8 bytes...>
where <len> is a typedstream variable-length integer:
    - a single byte < 0x80 is the length itself
    - 0x81 -> next 2 bytes are a little-endian uint16 length
    - 0x82 -> next 4 bytes are a little-endian uint32 length
"""

from __future__ import annotations


def _read_varint(data: bytes, i: int):
    """Read a typedstream length integer at offset i. Returns (value, next_i)."""
    b = data[i]
    if b == 0x81:
        return int.from_bytes(data[i + 1:i + 3], "little"), i + 3
    if b == 0x82:
        return int.from_bytes(data[i + 1:i + 5], "little"), i + 5
    return b, i + 1


def decode(blob) -> str | None:
    """Return the message text from an attributedBody blob, or None."""
    if not blob:
        return None
    if isinstance(blob, str):
        # Some drivers hand back text directly.
        return blob or None

    marker = b"NSString"
    idx = blob.find(marker)
    if idx == -1:
        return None

    # After "NSString" there's a small class/version preamble (e.g.
    # \x01\x94\x84\x01) and then the length-prefixed UTF-8 bytes. The exact
    # preamble width varies (new class vs. back-reference), so rather than
    # assume a fixed offset we try each plausible start, keep every candidate
    # that decodes as clean UTF-8, and return the LONGEST one. This avoids
    # latching onto a 1-byte preamble value (a classic false match) when the
    # real, longer message string is sitting a byte or two further along.
    pos = idx + len(marker)

    # Canonical fast path. After "NSString" comes a short class/version
    # preamble (\x01\x94\x84\x01 for a new class, \x01\x95\x84\x01 for a
    # back-reference, etc.), then a one-byte typedstream type marker \x2B
    # ('+', "byte array"), then the length-prefixed UTF-8 bytes. The preamble
    # never contains \x2B, so the first \x2B after "NSString" reliably marks
    # the type byte; the length varint follows immediately. Reading the length
    # at this exact offset avoids the off-by-one a fuzzy scan can hit (which
    # would prepend a stray byte, e.g. the type marker itself, to a message).
    tm = blob.find(b"\x2b", pos, pos + 12)
    if tm != -1:
        try:
            length, start = _read_varint(blob, tm + 1)
            if 0 < length and start + length <= len(blob):
                text = blob[start:start + length].decode("utf-8")
                if "\x00" not in text:
                    return text
        except (IndexError, UnicodeDecodeError):
            pass

    best = None
    for skip in range(1, 6):
        try:
            length, start = _read_varint(blob, pos + skip)
        except IndexError:
            continue
        if length <= 0 or start + length > len(blob):
            continue
        candidate = blob[start:start + length]
        try:
            text = candidate.decode("utf-8")
        except UnicodeDecodeError:
            continue
        # A real message decodes cleanly and isn't full of NULs.
        if "\x00" in text:
            continue
        if best is None or len(text) > len(best):
            best = text
    return best


if __name__ == "__main__":
    # Smoke test against the real database.
    import sqlite3
    import sys

    db = sys.argv[1] if len(sys.argv) > 1 else "data/chat.db"
    conn = sqlite3.connect(db)
    conn.text_factory = bytes  # we want raw bytes for attributedBody
    rows = conn.execute(
        "SELECT ROWID, text, attributedBody FROM message "
        "WHERE attributedBody IS NOT NULL LIMIT 25"
    ).fetchall()
    ok = 0
    for rowid, text, body in rows:
        decoded = decode(body)
        plain = text.decode("utf-8", "replace") if text else None
        shown = (decoded or "")[:80].replace("\n", " ")
        status = "OK " if decoded else "!! "
        if decoded:
            ok += 1
        print(f"{status}#{rowid}: {shown!r}")
    print(f"\nDecoded {ok}/{len(rows)} sampled rows.")
