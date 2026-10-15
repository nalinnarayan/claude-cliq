'use strict';
const test = require('node:test');
const assert = require('node:assert');
const crypto = require('crypto');
const { createRelay } = require('../functions/relay/core.js');

// Same in-memory shape core.test.js uses; duplicated rather than exported so
// the two suites cannot break each other.
function memStore() {
  const codes = new Map(), devices = new Map(), pending = new Map(), counters = new Map();
  return {
    async putPairCode(c, v) { codes.set(c, v); },
    async getPairCode(c) { return codes.get(c) || null; },
    async deletePairCode(c) { codes.delete(c); },
    async putDevice(h, v) { devices.set(h, { token_hash: h, ...v }); },
    async getDevice(h) { return devices.get(h) || null; },
    async deleteDevice(h) { devices.delete(h); },
    async putPending(m) { pending.set(m.id, m); },
    async listPending(u, since) {
      return [...pending.values()].filter((m) => m.cliq_user === u && m.ts > since);
    },
    async deletePending(rows) { for (const r of rows) pending.delete(r.id); },
    _pending: pending,
  };
}

function harness(opts = {}) {
  const store = memStore();
  const published = [];
  const streams = {
    configured: true,
    channelId: 'chan-1',
    async publishWake(w) { published.push(w); return true; },
    async tokenPair(conn) {
      return { url: 'in2-dms.zoho.in', key: 'k'.repeat(40), wss_id: `w-${conn}`, channel_id: 'chan-1' };
    },
  };
  const relay = createRelay({
    store,
    cliq: { async dm() {} },
    inboundSecret: 'topsecret',
    streams,
    streamSecret: 'stream-secret',
    ...opts,
  });
  return { relay, store, streams, published };
}

const secret = { 'x-relay-secret': 'topsecret' };
const bearer = (t) => ({ 'x-device-token': t });

async function pairUser(h, cliqUser, email) {
  const { code } = await h.relay.issuePairCode({ cliqUser, email });
  const { device_token } = await h.relay.pair({ body: { code, device_name: 'laptop' } });
  return device_token;
}

test('a reply rings the doorbell exactly once, with no message body', async () => {
  const h = harness();
  await h.relay.inbound({
    headers: secret,
    body: { cliq_user: 'u1', text: 'ship it', message_id: 'cliq-1' },
  });
  assert.strictEqual(h.published.length, 1, 'one wake per reply');
  const wake = h.published[0];
  // The whole point: a channel broadcasts to the org, so nothing but an
  // opaque label may cross it.
  assert.doesNotMatch(wake, /ship it/);
  assert.doesNotMatch(wake, /u1/);
  assert.match(wake, /^[0-9a-f]{32}$/, 'an opaque 128-bit hex label');
});

test('the wake label is per account, stable, and unguessable without the secret', async () => {
  const a = harness();
  const b = harness();                        // same streamSecret
  const c = harness({ streamSecret: 'different' });

  await a.relay.inbound({ headers: secret, body: { cliq_user: 'u1', text: 'x' } });
  await a.relay.inbound({ headers: secret, body: { cliq_user: 'u1', text: 'y' } });
  await a.relay.inbound({ headers: secret, body: { cliq_user: 'u2', text: 'z' } });
  await b.relay.inbound({ headers: secret, body: { cliq_user: 'u1', text: 'x' } });
  await c.relay.inbound({ headers: secret, body: { cliq_user: 'u1', text: 'x' } });

  assert.strictEqual(a.published[0], a.published[1], 'stable for one account');
  assert.notStrictEqual(a.published[0], a.published[2], 'different per account');
  assert.strictEqual(a.published[0], b.published[0], 'derivable, not stored');
  assert.notStrictEqual(a.published[0], c.published[0], 'depends on the secret');

  // It must not be the account id dressed up.
  const expected = crypto.createHmac('sha256', 'stream-secret')
    .update('wake|u1').digest('hex').slice(0, 32);
  assert.strictEqual(a.published[0], expected);
});

test('a failed publish does not fail the inbound', async () => {
  // Cliq does not redeliver, so a 500 here would lose the reply outright --
  // whereas a missed bell costs one extra fetch and nothing else.
  const h = harness();
  h.streams.publishWake = async () => { throw new Error('channel is on fire'); };
  const res = await h.relay.inbound({
    headers: secret, body: { cliq_user: 'u1', text: 'still mine' },
  });
  assert.deepStrictEqual(res, { ok: true });
  assert.strictEqual([...h.store._pending.values()].length, 1, 'the reply is queued regardless');
});

test('no channel configured means no bell, and still no error', async () => {
  const h = harness();
  h.streams.configured = false;
  const res = await h.relay.inbound({ headers: secret, body: { cliq_user: 'u1', text: 'hi' } });
  assert.deepStrictEqual(res, { ok: true });
  assert.strictEqual(h.published.length, 0);
});

test('a ticket needs a device token and carries that account wake id', async () => {
  const h = harness();
  await assert.rejects(() => h.relay.streamTicket({ headers: {} }), /device token/);
  await assert.rejects(() => h.relay.streamTicket({ headers: bearer('nonsense') }),
                       /unknown or revoked/);

  const token = await pairUser(h, 'u1', 'u1@example.com');
  const t = await h.relay.streamTicket({ headers: bearer(token) });
  assert.ok(t.key && t.url && t.wss_id, 'a usable ticket');

  // The wake id the laptop is told to listen for must be the one the relay
  // will actually publish for that account.
  await h.relay.inbound({ headers: secret, body: { cliq_user: 'u1', text: 'x' } });
  assert.strictEqual(t.wake_id, h.published[0]);
});

test('the connection name reveals neither the token nor the account', async () => {
  const h = harness();
  const token = await pairUser(h, 'u1', 'u1@example.com');
  const t = await h.relay.streamTicket({ headers: bearer(token) });
  const conn = t.wss_id.replace(/^w-/, '');          // the fake echoes it back
  // Exactly 20: Data Streams rejects a longer connection_name with an error
  // that names neither the field nor the limit, so pin it here rather than
  // rediscover it on a live channel.
  assert.match(conn, /^[0-9a-f]{20}$/);
  assert.ok(conn.length <= 20, 'Data Streams caps connection_name at 20');
  assert.ok(!conn.includes(token), 'not the token');
  assert.ok(!conn.includes('u1'), 'not the account');
  assert.notStrictEqual(conn, t.wake_id, 'and not the wake label either');
});

test('two accounts get different tickets and different wake ids', async () => {
  const h = harness();
  const t1 = await pairUser(h, 'u1', 'u1@example.com');
  const t2 = await pairUser(h, 'u2', 'u2@example.com');
  const a = await h.relay.streamTicket({ headers: bearer(t1) });
  const b = await h.relay.streamTicket({ headers: bearer(t2) });
  assert.notStrictEqual(a.wake_id, b.wake_id);
  assert.notStrictEqual(a.wss_id, b.wss_id);
});

test('without a stream secret there is no label, so no bell is rung', async () => {
  // Fail closed rather than publishing a guessable constant.
  const h = harness({ streamSecret: '' });
  await h.relay.inbound({ headers: secret, body: { cliq_user: 'u1', text: 'hi' } });
  assert.strictEqual(h.published.length, 0);
});
