"""Carrying the request id in a message without showing it.

A swipe-reply is bound by finding the request id in the text of the message
replied to -- and it has to come from the text, because v3 never tells us the
id of a message we sent (`sync_message` is rejected, so no id comes back) and
the relay deliberately stores nothing. `replied_message.text` hands back the
raw source, decoration included, which is what makes this possible at all.

Printing "(id atyxx)" worked but put bookkeeping in front of the reader. The
same five letters are encoded here as ZERO-WIDTH characters: present in the
text, absent from the screen.

Five lowercase letters, five bits each, twenty-five bits between two markers:

    U+2060 WORD JOINER            start and end marker
    U+200B ZERO WIDTH SPACE       0
    U+200C ZERO WIDTH NON-JOINER  1

The relay decodes the same scheme in `hidden.js`, so the two must agree
exactly; the round-trip test holds them together.
"""

from __future__ import annotations

MARK = "⁠"
ZERO = "​"
ONE = "‌"

ID_LENGTH = 5
BITS_PER_LETTER = 5


def encode(request_id: str) -> str:
    """The id as invisible characters, or "" if it is not encodable.

    Returning "" rather than raising matters: a message going out is worth
    more than its binding, so an id of an unexpected shape costs the swipe
    reply and nothing else.
    """
    rid = (request_id or "").strip().lower()
    if len(rid) != ID_LENGTH or not rid.isalpha() or not rid.isascii():
        return ""
    bits = "".join(f"{ord(c) - 97:0{BITS_PER_LETTER}b}" for c in rid)
    return MARK + "".join(ONE if b == "1" else ZERO for b in bits) + MARK


def decode(text: str) -> str:
    """The id hidden in `text`, or "" if there is not a well-formed one."""
    s = text or ""
    start = s.find(MARK)
    if start == -1:
        return ""
    end = s.find(MARK, start + 1)
    if end == -1:
        return ""
    bits = []
    for ch in s[start + 1:end]:
        if ch == ZERO:
            bits.append("0")
        elif ch == ONE:
            bits.append("1")
        else:
            return ""                    # anything else means it was mangled
    if len(bits) != ID_LENGTH * BITS_PER_LETTER:
        return ""
    out = []
    for i in range(0, len(bits), BITS_PER_LETTER):
        n = int("".join(bits[i:i + BITS_PER_LETTER]), 2)
        if n > 25:
            return ""
        out.append(chr(97 + n))
    return "".join(out)


def strip(text: str) -> str:
    """Remove the carrier, for anything that has to read like prose."""
    return (text or "").replace(MARK, "").replace(ZERO, "").replace(ONE, "")
