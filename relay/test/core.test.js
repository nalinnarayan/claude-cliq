'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { createRelay, sha256 } = require('../functions/relay/core.js');
const { buildForm } = require('../functions/relay/askform.js');

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

test('a notification writes nothing to the store', async () => {
  // It used to write twice -- a Sessions upsert and a send counter -- and
  // the UPDATE free tier is a tenth of the read tier, so two writes per
  // message blew it on their own. Pacing moved to the laptop; the Sessions
  // row had no reader once the daemon went.
  const h = harness();
  const tok = await pairUser(h, 'u1', 'alice@z.com');
  let writes = 0;
  for (const m of ['putSession', 'bumpCounter', 'putPending']) {
    h.store[m] = async () => { writes += 1; };
  }
  for (let i = 0; i < 50; i++) {
    await h.relay.notify({ headers: bearer(tok), body: { text: `m${i}` } });
  }
  assert.strictEqual(writes, 0, 'no writes, however many messages');
  assert.strictEqual(h.sent.length, 50, 'and nothing is shed relay-side');
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

test('a tapped button lands as a bound pending reply', async () => {
  const h = harness();
  const tok = await pairUser(h, 'u1', 'alice@z.com');
  const sec = { 'x-relay-secret': 'topsecret' };
  await h.relay.action({ headers: sec, body: { cliq_user: 'u1', req: 'abcde', ans: 'allow' } });
  const got = await h.relay.await_({ headers: bearer(tok), body: { wait: 0 }, sleep });
  assert.strictEqual(got.messages.length, 1);
  assert.strictEqual(got.messages[0].request_id, 'abcde');
  assert.strictEqual(got.messages[0].text, 'yes');
});

test('the chooser tap carries the id and no text', async () => {
  const h = harness();
  const tok = await pairUser(h, 'u1', 'alice@z.com');
  await h.relay.action({ headers: { 'x-relay-secret': 'topsecret' },
                         body: { cliq_user: 'u1', req: 'fghij', ans: 'pick' } });
  const got = await h.relay.await_({ headers: bearer(tok), body: { wait: 0 }, sleep });
  assert.strictEqual(got.messages[0].request_id, 'fghij');
  assert.strictEqual(got.messages[0].text, '');
});

test('a tap needs the shared secret and a known answer', async () => {
  const h = harness();
  await assert.rejects(() => h.relay.action({ headers: {}, body: { cliq_user: 'u1', req: 'a' } }),
                       /bad secret/);
  await assert.rejects(() => h.relay.action({
    headers: { 'x-relay-secret': 'topsecret' },
    body: { cliq_user: 'u1', req: 'abcde', ans: 'rm -rf' } }), /unknown answer/);
});

test("a tap only ever reaches the tapper's own devices", async () => {
  const h = harness();
  const tokA = await pairUser(h, 'u1', 'alice@z.com');
  const tokB = await pairUser(h, 'u2', 'bob@z.com');
  // Bob taps, naming a request id that belongs to Alice's session.
  await h.relay.action({ headers: { 'x-relay-secret': 'topsecret' },
                         body: { cliq_user: 'u2', req: 'abcde', ans: 'allow' } });
  const alice = await h.relay.await_({ headers: bearer(tokA), body: { wait: 0 }, sleep });
  assert.strictEqual(alice.messages.length, 0, 'must not reach Alice');
  const bob = await h.relay.await_({ headers: bearer(tokB), body: { wait: 0 }, sleep });
  assert.strictEqual(bob.messages.length, 1);
});

test('answers become buttons carrying the request id', () => {
  const { buildButtons } = require('../functions/relay/core.js');
  const btns = buildButtons('abcde', [
    { label: 'Allow', value: 'allow' }, { label: 'Deny', value: 'deny' },
  ], { fnName: 'claudeCliqAction', fnOwner: 'me@z.com' });
  assert.strictEqual(btns.length, 2);
  assert.deepStrictEqual(btns[0].arguments, { req: 'abcde', ans: 'allow' });
  assert.strictEqual(btns[0].action.type, 'invoke.function');
  assert.strictEqual(btns[0].action.data.name, 'claudeCliqAction');
  // Required for anyone who did not create the function.
  assert.strictEqual(btns[0].action.data.owner, 'me@z.com');
});

test('buttons are capped at five, and owner is omitted when unset', () => {
  const { buildButtons } = require('../functions/relay/core.js');
  const many = Array.from({ length: 9 }, (_, i) => ({ label: `o${i}`, value: `${i}` }));
  const btns = buildButtons('abcde', many, { fnName: 'f', fnOwner: '' });
  assert.strictEqual(btns.length, 5, 'Cliq allows at most five');
  assert.ok(!('owner' in btns[0].action.data));
});

test('the id is found however the hint decorated it', async () => {
  const h = harness();
  const tok = await pairUser(h, 'u1', 'alice@z.com');
  const sec = { 'x-relay-secret': 'topsecret' };
  const variants = [
    'reply to keep it going (id abcde)',
    'reply to keep it going (id `abcde`)',      // what the hook actually writes
    '_reply to keep it going (id `abcde`)_',    // plus the italics wrapper
    'something\nmultiline (id abcde)\nmore',
  ];
  for (const [i, replied] of variants.entries()) {
    await h.relay.inbound({ headers: sec,
      body: { cliq_user: 'u1', text: 'x', message_id: `v${i}`, replied_text: replied } });
  }
  const got = await h.relay.await_({ headers: bearer(tok), body: { wait: 0 }, sleep });
  assert.strictEqual(got.messages.length, variants.length);
  for (const m of got.messages) {
    assert.strictEqual(m.request_id, 'abcde', `failed for: ${m.id}`);
  }
});

test('a hidden id binds a swipe-reply with nothing shown to the reader', async () => {
  // The id used to be printed as "(id abcde)". It is now carried as
  // zero-width characters: present in the text that replied_message hands
  // back, absent from the screen. The visible form is still read, because
  // messages already in the chat carry it.
  const { execFileSync } = require('child_process');
  const path = require('path');
  const carrier = execFileSync('python3', ['-c', `
import sys
sys.path.insert(0, ${JSON.stringify(path.join(__dirname, '..', '..', 'scripts'))})
import hidden
print(hidden.encode("abcde"), end="")
`], { encoding: 'utf8' });

  const h = harness();
  const tok = await pairUser(h, 'u1', 'alice@z.com');
  const sec = { 'x-relay-secret': 'topsecret' };
  await h.relay.inbound({ headers: sec, body: {
    cliq_user: 'u1', text: 'go on', message_id: 'hidden-1',
    replied_text: `reply to keep it going${carrier}` } });

  const got = await h.relay.await_({ headers: bearer(tok), body: { wait: 0 }, sleep });
  assert.strictEqual(got.messages[0].request_id, 'abcde',
                     'bound with no id visible in the message');
});

test('a mangled carrier leaves the reply unbound rather than misbound', async () => {
  // If Cliq ever rewrites these characters, the reply must fall back to the
  // chooser -- never to a plausible-looking id that answers the wrong thing.
  const h = harness();
  const tok = await pairUser(h, 'u1', 'alice@z.com');
  await h.relay.inbound({ headers: { 'x-relay-secret': 'topsecret' }, body: {
    cliq_user: 'u1', text: 'go on', message_id: 'hidden-2',
    replied_text: `reply to keep it going\u2060\u200b\u200b\u2060` } });
  const got = await h.relay.await_({ headers: bearer(tok), body: { wait: 0 }, sleep });
  assert.strictEqual(got.messages[0].request_id, '', 'unbound, not guessed');
});

test('Leave it releases the question, the way a dismissal used to', async () => {
  // Dismissing the form no longer releases anything, so giving up needs a
  // button. It queues the SAME sentinel the cancel path produced, so the
  // laptop needs no new handling.
  const h = harness();
  const tok = await pairUser(h, 'u1', 'alice@z.com');
  await h.relay.action({ headers: { 'x-relay-secret': 'topsecret' },
                         body: { cliq_user: 'u1', req: 'abcde', ans: 'leave' } });
  const got = await h.relay.await_({ headers: bearer(tok), body: { wait: 0 }, sleep });
  assert.strictEqual(got.messages[0].request_id, 'abcde');
  assert.deepStrictEqual(JSON.parse(got.messages[0].text),
                         { ask: 'cancelled', picked: {} });
});

test('dismissing a form cannot reach us at all', () => {
  // trigger_on_cancel is false, so Cliq never calls the submit function on a
  // dismissal. Off rather than on-and-ignored, so there is no path to get
  // wrong: closing a form to re-read the question behind it must do nothing.
  const form = buildForm('abcde', [{ question: 'Q', options: ['A', 'B'] }]);
  assert.strictEqual(form.trigger_on_cancel, false);
});

test('a submission is not confirmed as delivered, only as sent', async () => {
  // The relay stores the answer and cannot know whether any hook is
  // listening -- waiters are laptop-side state it does not hold. "✓ Sent"
  // was confirmed to a user whose question had already gone back to the
  // terminal.
  const h = harness();
  const res = await h.relay.askSubmit({
    headers: { 'x-relay-secret': 'topsecret' },
    body: { cliq_user: 'u1', name: 'abcde', action: 'submit',
            values: { q0: { label: 'A', value: '0' } } },
  });
  assert.doesNotMatch(res.text, /✓/, 'no tick: nothing was confirmed');
  assert.match(res.text, /Sent to your session/);
});

test('peek shows what is queued without consuming it', async () => {
  const h = harness();
  const tok = await pairUser(h, 'u1', 'alice@z.com');
  await h.relay.inbound({ headers: { 'x-relay-secret': 'topsecret' },
                          body: { cliq_user: 'u1', text: 'still here', message_id: 'p1' } });
  const first = await h.relay.peek({ headers: bearer(tok) });
  assert.strictEqual(first.messages.length, 1);
  const second = await h.relay.peek({ headers: bearer(tok) });
  assert.strictEqual(second.messages.length, 1, 'peeking must not delete');
  const got = await h.relay.await_({ headers: bearer(tok), body: { wait: 0 }, sleep });
  assert.strictEqual(got.messages.length, 1, 'await still delivers it');
});
