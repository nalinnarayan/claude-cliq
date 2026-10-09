'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { createRelay, sha256 } = require('../functions/relay/core.js');

// In-memory stand-in for the Catalyst NoSQL tables.
function memStore() {
  const codes = new Map(), devices = new Map(), pending = new Map();
  const sessions = new Map(), counters = new Map();
  return {
    _pending: pending,
    async putPairCode(c, v) { codes.set(c, v); },
    async getPairCode(c) { return codes.get(c) || null; },
    async deletePairCode(c) { codes.delete(c); },
    async putDevice(h, v) { devices.set(h, v); },
    async getDevice(h) { return devices.get(h) || null; },
    async deleteDevice(h) { devices.delete(h); },
    async putPending(m) { pending.set(m.id, m); },
    async listPending(user, since) {
      return [...pending.values()]
        .filter((m) => m.cliq_user === user && m.ts > since)
        .sort((a, b) => a.ts - b.ts);
    },
    async deletePending(rows) { rows.forEach((r) => pending.delete(r.id)); },
    async putSession(user, s) { sessions.set(user, s); },
    async bumpCounter(k) { const n = (counters.get(k) || 0) + 1; counters.set(k, n); return n; },
  };
}

function harness(opts = {}) {
  const store = memStore();
  const sent = [];
  const cliq = { async dm(email, text) { sent.push({ email, text }); } };
  const relay = createRelay({
    store, cliq, inboundSecret: 'topsecret',
    debugUsers: opts.debugUsers || [], ...opts,
  });
  return { store, sent, relay };
}
const bearer = (t) => ({ 'x-device-token': t });
const sleep = () => Promise.resolve();

async function pairUser(h, cliqUser, email) {
  const { code } = await h.relay.issuePairCode({ cliqUser, email });
  const { device_token } = await h.relay.pair({ body: { code, device_name: 'mac' } });
  return device_token;
}

test('pairing code is single use', async () => {
  const h = harness();
  const { code } = await h.relay.issuePairCode({ cliqUser: 'u1', email: 'a@z.com' });
  await h.relay.pair({ body: { code } });
  await assert.rejects(() => h.relay.pair({ body: { code } }), /unknown code/);
});

test('expired pairing code is refused', async () => {
  let t = 1_000_000;
  const h = harness({ now: () => t });
  const { code } = await h.relay.issuePairCode({ cliqUser: 'u1', email: 'a@z.com' });
  t += 11 * 60 * 1000;
  await assert.rejects(() => h.relay.pair({ body: { code } }), /expired/);
});

test('notify requires a valid device token', async () => {
  const h = harness();
  await assert.rejects(() => h.relay.notify({ headers: {}, body: { text: 'hi' } }),
                       /missing device token/);
  await assert.rejects(
    () => h.relay.notify({ headers: bearer('garbage'), body: { text: 'hi' } }),
    /unknown or revoked/);
});

test('notify sends to the token owner, never to a body-supplied address', async () => {
  const h = harness();
  const tok = await pairUser(h, 'u1', 'alice@z.com');
  await h.relay.notify({
    headers: bearer(tok),
    // A hostile client tries to redirect the DM. These must be ignored.
    body: { text: 'hello', email: 'mallory@z.com', cliq_user: 'u2', to: 'bob@z.com' },
  });
  assert.strictEqual(h.sent.length, 1);
  assert.strictEqual(h.sent[0].email, 'alice@z.com');
});

test('a device only ever reads its own owner\'s replies', async () => {
  const h = harness();
  const tokA = await pairUser(h, 'u1', 'alice@z.com');
  const tokB = await pairUser(h, 'u2', 'bob@z.com');
  const sec = { 'x-relay-secret': 'topsecret' };
  await h.relay.inbound({ headers: sec, body: { cliq_user: 'u1', text: 'for alice', message_id: 'm1' } });
  await h.relay.inbound({ headers: sec, body: { cliq_user: 'u2', text: 'for bob', message_id: 'm2' } });

  // Bob's device asks for Alice's messages by naming her in the body.
  const bobSees = await h.relay.await_({
    headers: bearer(tokB), body: { since: 0, wait: 0, cliq_user: 'u1' }, sleep,
  });
  assert.deepStrictEqual(bobSees.messages.map((m) => m.text), ['for bob']);

  const aliceSees = await h.relay.await_({ headers: bearer(tokA), body: { since: 0, wait: 0 }, sleep });
  assert.deepStrictEqual(aliceSees.messages.map((m) => m.text), ['for alice']);
});

test('delivered messages are removed', async () => {
  const h = harness();
  const tok = await pairUser(h, 'u1', 'alice@z.com');
  await h.relay.inbound({ headers: { 'x-relay-secret': 'topsecret' },
                          body: { cliq_user: 'u1', text: 'once', message_id: 'm1' } });
  const first = await h.relay.await_({ headers: bearer(tok), body: { wait: 0 }, sleep });
  assert.strictEqual(first.messages.length, 1);
  const second = await h.relay.await_({ headers: bearer(tok), body: { wait: 0 }, sleep });
  assert.strictEqual(second.messages.length, 0);
});

test('inbound rejects a wrong or missing secret', async () => {
  const h = harness();
  await assert.rejects(() => h.relay.inbound({ headers: {}, body: { cliq_user: 'u1', text: 'x' } }), /bad secret/);
  await assert.rejects(
    () => h.relay.inbound({ headers: { 'x-relay-secret': 'nope' }, body: { cliq_user: 'u1', text: 'x' } }),
    /bad secret/);
});

test('inbound is idempotent on message id', async () => {
  const h = harness();
  const sec = { 'x-relay-secret': 'topsecret' };
  const body = { cliq_user: 'u1', text: 'dup', message_id: 'same' };
  await h.relay.inbound({ headers: sec, body });
  await h.relay.inbound({ headers: sec, body });
  assert.strictEqual(h.store._pending.size, 1);
});

test('send budget protects the shared Cliq rate limit', async () => {
  const h = harness();
  const tok = await pairUser(h, 'u1', 'alice@z.com');
  for (let i = 0; i < 40; i++) {
    await h.relay.notify({ headers: bearer(tok), body: { text: `m${i}` } });
  }
  await assert.rejects(
    () => h.relay.notify({ headers: bearer(tok), body: { text: 'one too many' } }),
    /send budget/);
});

test('bodies are retained only for the debug allowlist', async () => {
  const h = harness({ debugUsers: ['u1'] });
  const sec = { 'x-relay-secret': 'topsecret' };
  await h.relay.inbound({ headers: sec, body: { cliq_user: 'u1', text: 'mine', message_id: 'a' } });
  await h.relay.inbound({ headers: sec, body: { cliq_user: 'u2', text: 'theirs', message_id: 'b' } });
  assert.strictEqual(h.store._pending.get('a').retain, true);
  assert.strictEqual(h.store._pending.get('b').retain, false);
});

test('revoked device loses access immediately', async () => {
  const h = harness();
  const tok = await pairUser(h, 'u1', 'alice@z.com');
  await h.relay.unpair({ headers: bearer(tok) });
  await assert.rejects(() => h.relay.notify({ headers: bearer(tok), body: { text: 'hi' } }),
                       /unknown or revoked/);
});
