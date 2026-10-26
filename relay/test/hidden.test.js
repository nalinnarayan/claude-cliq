'use strict';
const test = require('node:test');
const assert = require('node:assert');
const path = require('path');
const { execFileSync } = require('child_process');
const { decode, strip, MARK, ZERO, ONE } = require('../functions/relay/hidden.js');

const SCRIPTS = path.join(__dirname, '..', '..', 'scripts');

/** Encode through the REAL laptop-side encoder, so the two cannot drift. */
function encodeInPython(rid) {
  return execFileSync('python3', ['-c', `
import sys
sys.path.insert(0, ${JSON.stringify(SCRIPTS)})
import hidden
print(hidden.encode(sys.argv[1]), end="")
`, rid], { encoding: 'utf8' });
}

test('an id written by the laptop is read back here', () => {
  // The two halves live in different languages, so the round trip is the only
  // thing holding the encoding together.
  for (const rid of ['atyxx', 'aaaaa', 'zzzzz', 'abcde', 'qwert']) {
    assert.strictEqual(decode(`reply to keep it going${encodeInPython(rid)}`), rid, rid);
  }
});

test('the carrier is invisible: nothing renders', () => {
  const carrier = encodeInPython('atyxx');
  assert.strictEqual(strip(carrier), '', 'no printable character at all');
  // 5 letters x 5 bits, plus two markers.
  assert.strictEqual([...carrier].length, 27);
});

test('text around the carrier is untouched', () => {
  const text = `✅ Claude finished\n\nbody here${encodeInPython('atyxx')}`;
  assert.strictEqual(strip(text), '✅ Claude finished\n\nbody here');
  assert.strictEqual(decode(text), 'atyxx');
});

test('no carrier means no id, not a wrong one', () => {
  assert.strictEqual(decode('reply to keep it going (id `atyxx`)'), '',
                     'the visible form is NOT this decoder\'s business');
  assert.strictEqual(decode(''), '');
  assert.strictEqual(decode(null), '');
  assert.strictEqual(decode('plain prose with no markers'), '');
});

test('a mangled carrier yields nothing rather than a guess', () => {
  // If Cliq ever strips or rewrites these, a half-read carrier must not
  // produce a plausible-looking id that binds a reply to the wrong request.
  const good = encodeInPython('atyxx');
  assert.strictEqual(decode(good.slice(0, 10)), '', 'truncated');
  assert.strictEqual(decode(MARK + ZERO.repeat(24) + MARK), '', 'too few bits');
  assert.strictEqual(decode(MARK + ZERO.repeat(26) + MARK), '', 'too many bits');
  assert.strictEqual(decode(MARK + ZERO.repeat(12) + 'x' + ZERO.repeat(12) + MARK), '',
                     'a stray visible character');
  assert.strictEqual(decode(MARK + ONE.repeat(25) + MARK), '',
                     'all ones is out of the letter range');
});

test('an unencodable id costs the binding, not the message', () => {
  // encode() returns empty rather than raising: a message going out is worth
  // more than its swipe-reply.
  assert.strictEqual(encodeInPython('TOOLONG'), '');
  assert.strictEqual(encodeInPython(''), '');
  assert.strictEqual(encodeInPython('ab1de'), '');
});
