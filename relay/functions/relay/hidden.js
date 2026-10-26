'use strict';
/**
 * Reading the request id a message carries invisibly.
 *
 * The laptop writes it in `scripts/hidden.py`; this decodes the same scheme,
 * so the two must agree exactly. The round-trip test runs the real Python
 * encoder against this decoder for that reason.
 *
 * Five lowercase letters, five bits each, twenty-five bits between two
 * markers:
 *
 *   U+2060 WORD JOINER            start and end marker
 *   U+200B ZERO WIDTH SPACE       0
 *   U+200C ZERO WIDTH NON-JOINER  1
 *
 * Why it can work at all: `replied_message.text` hands back the raw source,
 * decoration included -- the same property the visible "(id xxxxx)" form
 * relied on. Why it is needed: v3 never reports the id of a message we sent,
 * so the id cannot be looked up and has to travel in the text.
 */

const MARK = '⁠';
const ZERO = '​';
const ONE = '‌';

const ID_LENGTH = 5;
const BITS_PER_LETTER = 5;

/** The id hidden in `text`, or '' if there is not a well-formed one. */
function decode(text) {
  const s = String(text || '');
  const start = s.indexOf(MARK);
  if (start === -1) return '';
  const end = s.indexOf(MARK, start + 1);
  if (end === -1) return '';

  let bits = '';
  for (const ch of s.slice(start + 1, end)) {
    if (ch === ZERO) bits += '0';
    else if (ch === ONE) bits += '1';
    else return '';                 // anything else means it was mangled
  }
  if (bits.length !== ID_LENGTH * BITS_PER_LETTER) return '';

  let out = '';
  for (let i = 0; i < bits.length; i += BITS_PER_LETTER) {
    const n = parseInt(bits.slice(i, i + BITS_PER_LETTER), 2);
    if (n > 25) return '';
    out += String.fromCharCode(97 + n);
  }
  return out;
}

/** Remove the carrier, for anything that has to read like prose. */
function strip(text) {
  return String(text || '').split(MARK).join('').split(ZERO).join('')
    .split(ONE).join('');
}

module.exports = { decode, strip, MARK, ZERO, ONE };
