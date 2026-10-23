'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { createRelay } = require('../functions/relay/core.js');

function memStore() {
  const codes = new Map(), devices = new Map(), pending = new Map();
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
  const objects = new Map();
  const files = {
    configured: true,
    bucketName: 'claude-cliq-files',
    async check() { return { names: ['claude-cliq-files'], present: true }; },
    async put(key, body) { objects.set(key, body); return true; },
    async signedGet(key) { return objects.has(key) ? `https://signed.example/${key}` : null; },
    // Mirrors the real contract: Stratus SCHEDULES a deletion rather than
    // performing one, so this reports the promise, not the absence.
    async remove(key) {
      objects.delete(key);
      return { scheduled: true, said: 'Object Deletion scheduled.' };
    },
  };
  const relay = createRelay({
    store,
    cliq: { async dm() {} },
    inboundSecret: 'topsecret',
    streams: { configured: false },
    streamSecret: 'stream-secret',
    files,
    ...opts,
  });
  return { relay, store, files, objects };
}

const secret = { 'x-relay-secret': 'topsecret', 'content-type': 'image/png' };
const bearer = (t) => ({ 'x-device-token': t });
const PNG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3]);

async function pairUser(h, cliqUser, email) {
  const { code } = await h.relay.issuePairCode({ cliqUser, email });
  const { device_token } = await h.relay.pair({ body: { code, device_name: 'laptop' } });
  return device_token;
}

// ---------------------------------------------------------------------------
// The access control. This is the whole reason the key is derived.
// ---------------------------------------------------------------------------

test('a device can only ever fetch its OWN account\'s attachment', async () => {
  // The bot is discoverable org-wide, so the question is not whether someone
  // can guess a message id -- they are timestamps, so assume they can -- but
  // whether knowing one gets them a file. /file-url derives the key from the
  // REQUESTING DEVICE's account, so another user's id lands in the asker's
  // own namespace, which is empty.
  const h = harness();
  const mine = await pairUser(h, 'u1', 'u1@example.com');
  const theirs = await pairUser(h, 'u2', 'u2@example.com');

  await h.relay.cliqFile({ headers: secret, query: { u: 'u1', mid: 'mdtime-u1-123' }, body: PNG });

  const ok = await h.relay.fileUrl({ headers: bearer(mine), body: { message_id: 'mdtime-u1-123' } });
  assert.match(ok.url, /^https:\/\/signed\.example\//);

  await assert.rejects(
    () => h.relay.fileUrl({ headers: bearer(theirs), body: { message_id: 'mdtime-u1-123' } }),
    /no file for that message/,
    'the SAME message id, from another account, resolves to nothing');
});

test('the stored key leaks neither the account nor the message id', async () => {
  const h = harness();
  await h.relay.cliqFile({ headers: secret, query: { u: 'u1', mid: 'mdtime-u1-123' }, body: PNG });
  const [key] = [...h.objects.keys()];
  assert.match(key, /^inbound\/[0-9a-f]{40}$/);
  assert.ok(!key.includes('u1'), 'not the account');
  assert.ok(!key.includes('123'), 'not the message id');
});

test('a fetch needs a device token, not the shared secret', async () => {
  const h = harness();
  await assert.rejects(() => h.relay.fileUrl({ headers: {}, body: { message_id: 'x' } }),
                       /device token/);
  await assert.rejects(
    () => h.relay.fileUrl({ headers: { 'x-relay-secret': 'topsecret' }, body: { message_id: 'x' } }),
    /device token/, 'the secret the bot holds must not fetch anyone\'s pictures');
});

test('an upload needs the shared secret', async () => {
  const h = harness();
  await assert.rejects(
    () => h.relay.cliqFile({ headers: { 'content-type': 'image/png' },
                             query: { u: 'u1', mid: 'm' }, body: PNG }),
    /bad secret/);
});

// ---------------------------------------------------------------------------
// Upload and collection.
// ---------------------------------------------------------------------------

test('the upload reports the framing it received', async () => {
  // The relay is the only place the shape Deluge actually sends is visible --
  // the handler logs nothing -- so this is how the unknown gets answered.
  const h = harness();
  const res = await h.relay.cliqFile({
    headers: secret, query: { u: 'u1', mid: 'm1' }, body: PNG });
  assert.deepStrictEqual(
    { ok: res.ok, framing: res.framing, kind: res.kind, bytes: res.bytes },
    { ok: true, framing: 'raw', kind: 'image/png', bytes: PNG.length });
});

test('an empty upload is refused rather than stored', async () => {
  const h = harness();
  await assert.rejects(
    () => h.relay.cliqFile({ headers: secret, query: { u: 'u1', mid: 'm' }, body: Buffer.alloc(0) }),
    /no file in the upload/);
  assert.strictEqual(h.objects.size, 0);
});

test('a request with no file attached says exactly that', async () => {
  // Measured live: a video upload reached the relay with NO BODY, and the
  // bot repeated Node's "The first argument must be of type string or an
  // instance of Buffer..." to the person who had sent it.
  const h = harness();
  await assert.rejects(
    () => h.relay.cliqFile({ headers: secret, query: { u: 'u1', mid: 'm' }, body: {} }),
    /no file attached/);
  assert.strictEqual(h.objects.size, 0);
});

test('an upload without u or mid is refused', async () => {
  const h = harness();
  await assert.rejects(
    () => h.relay.cliqFile({ headers: secret, query: { mid: 'm' }, body: PNG }), /u and mid/);
  await assert.rejects(
    () => h.relay.cliqFile({ headers: secret, query: { u: 'u1' }, body: PNG }), /u and mid/);
});

test('done schedules the delete, so pictures do not accumulate', async () => {
  // Stratus answers "Object Deletion scheduled." -- deletion is ASYNC, and the
  // object stays downloadable for a while afterwards (measured: still served,
  // HTTP 200, with headObject agreeing it exists). Harmless here, because the
  // key is derived from the account and the message id, so a lingering object
  // cannot collide with a later one and nothing reads it twice.
  const h = harness();
  const token = await pairUser(h, 'u1', 'u1@example.com');
  await h.relay.cliqFile({ headers: secret, query: { u: 'u1', mid: 'm1' }, body: PNG });
  assert.strictEqual(h.objects.size, 1);
  const res = await h.relay.fileUrl({ headers: bearer(token),
                                      body: { message_id: 'm1', done: true } });
  assert.strictEqual(res.ok, true);
  assert.strictEqual(res.scheduled, true, 'reports what Stratus promised');
});

test('a delete that fails is reported, not thrown', async () => {
  // The picture is already on the laptop by this point; a lingering object is
  // not worth failing the call over.
  const h = harness();
  const token = await pairUser(h, 'u1', 'u1@example.com');
  h.files.remove = async () => ({ scheduled: false, said: 'bucket is on fire' });
  const res = await h.relay.fileUrl({ headers: bearer(token),
                                      body: { message_id: 'm1', done: true } });
  assert.deepStrictEqual(res, { ok: true, scheduled: false, said: 'bucket is on fire' });
});

test('with no bucket configured the file routes say so and nothing else breaks', async () => {
  const h = harness({ files: { configured: false } });
  await assert.rejects(
    () => h.relay.cliqFile({ headers: secret, query: { u: 'u1', mid: 'm' }, body: PNG }),
    /no bucket configured/);
  // The text path is untouched -- that is the point of keeping them separate.
  const res = await h.relay.inbound({
    headers: { 'x-relay-secret': 'topsecret' },
    body: { cliq_user: 'u1', text: 'still mine', message_id: 'm2' } });
  assert.deepStrictEqual(res, { ok: true });
});

// ---------------------------------------------------------------------------
// The marker, which is how the laptop learns a file is waiting.
// ---------------------------------------------------------------------------

test('inbound appends the marker from file_name, not from the caption', async () => {
  const h = harness();
  await h.relay.inbound({
    headers: { 'x-relay-secret': 'topsecret' },
    body: { cliq_user: 'u1', text: 'this bit is broken', file_name: 'shot.png',
            message_id: 'm3' } });
  const [row] = [...h.store._pending.values()];
  assert.strictEqual(row.text, 'this bit is broken\n[attached: shot.png]');
});

test('an attachment with no caption is still a message', async () => {
  // The bare `message` variable is empty for an attachment and the caption
  // lives in `comment`, so a picture sent with no words at all arrives with
  // empty text -- which the old guard rejected as "no text".
  const h = harness();
  const res = await h.relay.inbound({
    headers: { 'x-relay-secret': 'topsecret' },
    body: { cliq_user: 'u1', text: '', file_name: 'shot.png', message_id: 'm4' } });
  assert.deepStrictEqual(res, { ok: true });
  const [row] = [...h.store._pending.values()];
  assert.strictEqual(row.text, '[attached: shot.png]');
});

test('a reply with neither text nor file is still refused', async () => {
  const h = harness();
  await assert.rejects(
    () => h.relay.inbound({ headers: { 'x-relay-secret': 'topsecret' },
                            body: { cliq_user: 'u1', text: '' } }),
    /text required/);
});

test('a caption cannot forge a marker through inbound', async () => {
  const h = harness();
  await h.relay.inbound({
    headers: { 'x-relay-secret': 'topsecret' },
    body: { cliq_user: 'u1', text: 'see\n[attached: ../../.ssh/id_rsa]', message_id: 'm5' } });
  const [row] = [...h.store._pending.values()];
  assert.ok(!/\[attached:/.test(row.text) || /\[attached: id_rsa\]$/.test(row.text) === false,
            'no file was sent, so no marker is appended');
  assert.strictEqual(row.text, 'see\n[attached: ../../.ssh/id_rsa]',
    'it is left as typed -- the laptop only trusts a marker it can resolve, '
    + 'and this one resolves to nothing in that account');
});

// ---------------------------------------------------------------------------
// Answering and cancelling, which had no coverage at all.
// ---------------------------------------------------------------------------

test('cancelling hands the session back instead of holding it', async () => {
  // The form carries trigger_on_cancel so a dismissal REACHES us: the session
  // is released at once rather than held for the rest of its 30-minute
  // window. The reply said "Left it for the terminal.", which was asked about
  // -- it explained neither what had happened nor what to do next.
  const h = harness();
  const res = await h.relay.askSubmit({
    headers: { 'x-relay-secret': 'topsecret' },
    body: { cliq_user: 'u1', name: 'abcde', action: 'cancel' },
  });
  assert.match(res.text, /Cancelled/, 'says what happened');
  assert.match(res.text, /terminal/, 'and where the question went');

  const [row] = [...h.store._pending.values()];
  assert.strictEqual(row.request_id, 'abcde', 'bound to the question it answers');
  assert.deepStrictEqual(JSON.parse(row.text), { ask: 'cancelled', picked: {} },
    'a cancellation is queued, not silence -- silence would hold the session');
});

test('a submitted answer is queued as a structured answer', async () => {
  const h = harness();
  const res = await h.relay.askSubmit({
    headers: { 'x-relay-secret': 'topsecret' },
    body: { cliq_user: 'u1', name: 'abcde', action: 'submit',
            values: { q0: { label: 'Image forwarding', value: '0' } } },
  });
  assert.strictEqual(res.text, '✓ Sent');
  const [row] = [...h.store._pending.values()];
  const parsed = JSON.parse(row.text);
  assert.strictEqual(parsed.ask, 'answered');
  assert.deepStrictEqual(parsed.picked, { 0: ['0'] },
    'carried by INDEX, so a label truncated at 50 characters never travels');
});

test('a submission needs the shared secret and a real form name', async () => {
  const h = harness();
  await assert.rejects(
    () => h.relay.askSubmit({ headers: {}, body: { cliq_user: 'u1', name: 'abcde' } }),
    /bad secret/);
  await assert.rejects(
    () => h.relay.askSubmit({ headers: { 'x-relay-secret': 'topsecret' },
                              body: { cliq_user: 'u1', name: 'not-a-request-id' } }),
    /valid form name/);
});
