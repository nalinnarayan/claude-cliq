'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { splitForCliq, MAX_PART, MAX_PARTS } = require('../functions/relay/format.js');

const para = (n, len = 200) =>
  Array.from({ length: n }, (_, i) => `Paragraph ${i}. ${'x'.repeat(len)}`).join('\n\n');

test('a short message is one part and is not touched', () => {
  assert.deepStrictEqual(splitForCliq('just a line'), { parts: ['just a line'], dropped: 0 });
  assert.deepStrictEqual(splitForCliq(''), { parts: [], dropped: 0 });
});

test('nothing is lost: the parts rejoin to the original', () => {
  // The whole point. The old behaviour cut at 1400 characters and appended an
  // ellipsis, so the end of a message -- where the point usually is -- was
  // gone with no way to ask for it.
  const text = para(40);
  const { parts, dropped } = splitForCliq(text);
  assert.ok(parts.length > 1, 'it did split');
  assert.strictEqual(dropped, 0);
  assert.strictEqual(parts.join('\n\n'), text, 'rejoined exactly');
});

test('every part fits inside a Cliq message', () => {
  for (const text of [para(60), para(3, 9000), 'a'.repeat(30000)]) {
    const { parts } = splitForCliq(text);
    for (const p of parts) {
      assert.ok(p.length <= MAX_PART, `${p.length} > ${MAX_PART}`);
    }
  }
});

test('it breaks at a blank line rather than mid-sentence', () => {
  const { parts } = splitForCliq(para(40));
  for (const p of parts.slice(1)) {
    assert.match(p, /^Paragraph \d+\./, 'each part starts a paragraph');
  }
});

test('a paragraph too long for one part is broken by line, then by word', () => {
  const lines = Array.from({ length: 300 }, (_, i) => `line ${i} ${'y'.repeat(40)}`).join('\n');
  const { parts } = splitForCliq(lines);
  assert.ok(parts.length > 1);
  for (const p of parts) assert.ok(p.length <= MAX_PART);
  // No word is split in half when there was a space to break on.
  assert.ok(!/\sy{1,39}$/.test(parts[0]), 'cut on a space, not inside a run');
});

test('a single unbroken run is cut hard rather than dropped', () => {
  const { parts, dropped } = splitForCliq('a'.repeat(9000));
  assert.strictEqual(parts.join('').length + dropped, 9000, 'all of it accounted for');
});

test('a code fence split across parts is closed and reopened', () => {
  // Otherwise everything after the break renders as one code block.
  const code = ['```', ...Array.from({ length: 200 }, (_, i) => `const x${i} = ${i};`), '```']
    .join('\n');
  const { parts } = splitForCliq(code, { limit: 1200 });
  assert.ok(parts.length > 1);
  for (const p of parts) {
    const fences = p.split('\n').filter((l) => l.trim().startsWith('```')).length;
    assert.strictEqual(fences % 2, 0, 'each part is fence-balanced on its own');
  }
});

test('beyond the part ceiling the overflow is counted, not hidden', () => {
  // Cliq's 50/min is shared across the deployment and a 429 locks everyone out
  // for ten minutes, so the number of messages has to be bounded -- but what
  // does not fit is reported rather than silently dropped.
  const { parts, dropped } = splitForCliq(para(400));
  assert.strictEqual(parts.length, MAX_PARTS);
  assert.ok(dropped > 0, 'and the caller can say how much');
});

test('the limit is configurable, because the hint has to fit too', () => {
  const { parts } = splitForCliq(para(20), { limit: 1000 });
  for (const p of parts) assert.ok(p.length <= 1000);
});

// ---------------------------------------------------------------------------
// What notify() actually sends. Several messages, in order, extras on the last.
// ---------------------------------------------------------------------------

const { createRelay } = require('../functions/relay/core.js');

function harness() {
  const devices = new Map(), codes = new Map(), pending = new Map();
  const sent = [];
  const relay = createRelay({
    store: {
      async putPairCode(c, v) { codes.set(c, v); },
      async getPairCode(c) { return codes.get(c) || null; },
      async deletePairCode(c) { codes.delete(c); },
      async putDevice(h, v) { devices.set(h, { token_hash: h, ...v }); },
      async getDevice(h) { return devices.get(h) || null; },
      async putPending(m) { pending.set(m.id, m); },
      async listPending() { return []; },
      async deletePending() {},
    },
    cliq: { async dm(to, text, extras) { sent.push({ to, text, extras }); } },
    inboundSecret: 'topsecret',
    streams: { configured: false },
    streamSecret: 'stream-secret',
  });
  return { relay, sent };
}

async function paired(h) {
  const { code } = await h.relay.issuePairCode({ cliqUser: 'u1', email: 'u1@example.com' });
  const { device_token } = await h.relay.pair({ body: { code, device_name: 'laptop' } });
  return { 'x-device-token': device_token };
}

test('a long turn arrives as several messages, in order, nothing cut', async () => {
  const h = harness();
  const headers = await paired(h);
  const text = para(40);
  await h.relay.notify({ headers, body: { text, request_id: 'abcde' } });

  assert.ok(h.sent.length > 1, 'more than one message');
  const bodies = h.sent.map((m) => m.text);
  bodies.forEach((b, i) => {
    assert.ok(b.startsWith(`_(${i + 1}/${bodies.length})_`),
              'each says where it sits in the sequence');
  });
  const rejoined = bodies.map((b) => b.replace(/^_\(\d+\/\d+\)_\n\n/, '')).join('\n\n');
  assert.strictEqual(rejoined, text, 'and together they are the whole message');
});

test('the hint rides on the LAST message, never the first', async () => {
  // It carries the "(id xxxxx)" a swipe-reply binds to. Appended before the
  // split it would have travelled with the prose -- and been the first thing
  // lost when a message overflowed, breaking replies on the longest ones.
  const h = harness();
  const headers = await paired(h);
  await h.relay.notify({ headers, body: { text: para(40),
                                          hint: 'reply to keep it going (id "abcde")' } });
  const last = h.sent[h.sent.length - 1].text;
  assert.match(last, /reply to keep it going \(id "abcde"\)_$/);
  for (const m of h.sent.slice(0, -1)) {
    assert.doesNotMatch(m.text, /keep it going/, 'not on the earlier parts');
  }
});

test('buttons are attached once, to the last message', async () => {
  // Otherwise a three-part turn offers the same question three times.
  const h = harness();
  const headers = await paired(h);
  await h.relay.notify({ headers, body: {
    text: para(40), request_id: 'abcde',
    answers: [{ label: 'Answer', value: 'form', req: 'abcde' }] } });
  const withButtons = h.sent.filter((m) => m.extras && m.extras.buttons);
  assert.strictEqual(withButtons.length, 1);
  assert.strictEqual(withButtons[0], h.sent[h.sent.length - 1]);
});

test('a short turn is still exactly one message, with no part marker', async () => {
  const h = harness();
  const headers = await paired(h);
  await h.relay.notify({ headers, body: { text: 'done', hint: 'reply (id "abcde")' } });
  assert.strictEqual(h.sent.length, 1);
  assert.strictEqual(h.sent[0].text, 'done\n\n_reply (id "abcde")_');
});

test('an overlong turn says how much did not fit', async () => {
  const h = harness();
  const headers = await paired(h);
  await h.relay.notify({ headers, body: { text: para(400) } });
  const last = h.sent[h.sent.length - 1].text;
  assert.match(last, /more characters did not fit/);
  assert.match(last, /at the terminal/, 'and where to find the rest');
});
