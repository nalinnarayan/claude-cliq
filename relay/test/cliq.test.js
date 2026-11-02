'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { createCliq, ALLOWED_KEYS } = require('../functions/relay/cliq.js');

/**
 * The wire shape of an outbound Cliq call.
 *
 * This file exists because its absence cost a month of silent misdelivery.
 * Every other test drives a hand-written stand-in for dm(), which records
 * whatever arguments it is handed -- so a recipient in the wrong position, or
 * under the wrong name, looked identical to a correct one. The live API
 * answered 200 either way and delivered to the token's owner.
 *
 * None of this proves delivery, which no unit test can reach. It proves the
 * request matches the shape that was measured delivering.
 */

function recorder(status = 200, body = '{"user_ids":["u1"]}') {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init, parsed: new URL(url) });
    return { ok: status >= 200 && status < 300, status, async text() { return body; } };
  };
  return { calls, fetchImpl };
}

const cliqWith = (fetchImpl, over = {}) => createCliq({
  baseUrl: 'https://cliq.zoho.in', botName: 'testbot', zapikey: 'zk-1',
  fetchImpl, ...over,
});

test('dm posts to the v2 message endpoint', async () => {
  const r = recorder();
  await cliqWith(r.fetchImpl).dm('50000000001', 'hello');
  assert.strictEqual(r.calls.length, 1);
  const { parsed, init } = r.calls[0];
  assert.strictEqual(parsed.pathname, '/api/v2/bots/testbot/message',
                     'v2 /message -- v3 /messages ignores the recipient');
  assert.strictEqual(init.method, 'POST');
  assert.strictEqual(init.headers['Content-Type'], 'application/json');
  assert.strictEqual(parsed.searchParams.get('zapikey'), 'zk-1');
});

test('the recipient rides in the body as `userids`, never in the query', async () => {
  const r = recorder();
  await cliqWith(r.fetchImpl).dm('50000000001', 'hello');
  const { parsed, init } = r.calls[0];
  const body = JSON.parse(init.body);

  // The whole bug, pinned from three directions.
  assert.strictEqual(body.userids, '50000000001', 'in the body, no underscore');
  assert.strictEqual(body.user_ids, undefined,
                     '`user_ids` is v3 spelling; v2 does not recognise it');
  assert.strictEqual(parsed.searchParams.get('user_ids'), null,
                     'a recipient in the query string is silently discarded');
  assert.strictEqual(parsed.searchParams.get('userids'), null);
  assert.strictEqual(body.text, 'hello');
});

test('a numeric zuid is sent as a string', async () => {
  // Callers pass whatever the device row holds; JSON would otherwise carry a
  // bare number and Cliq expects the id as text.
  const r = recorder();
  await cliqWith(r.fetchImpl).dm(50000000001, 'hello');
  assert.strictEqual(JSON.parse(r.calls[0].init.body).userids, '50000000001');
});

test('slides and buttons pass through; anything else is dropped', async () => {
  const r = recorder();
  const slides = [{ type: 'table', data: { headers: ['a'], rows: [{ a: '1' }] } }];
  const buttons = [{ label: 'yes', action: { type: 'invoke.function' } }];
  await cliqWith(r.fetchImpl).dm('u1', 'pick', {
    slides, buttons, styles: { markdown: true }, nonsense: 1,
  });
  const body = JSON.parse(r.calls[0].init.body);
  assert.deepStrictEqual(body.slides, slides);
  assert.deepStrictEqual(body.buttons, buttons);
  // v2 would ignore these rather than reject the request, which is exactly
  // why the filter needs a test now -- v3's hard 400 used to be the check.
  assert.strictEqual(body.styles, undefined);
  assert.strictEqual(body.nonsense, undefined);
  assert.deepStrictEqual(ALLOWED_KEYS, ['slides', 'buttons']);
});

test('an absent extra is not sent as null', async () => {
  const r = recorder();
  await cliqWith(r.fetchImpl).dm('u1', 'hi', { slides: null, buttons: undefined });
  const body = JSON.parse(r.calls[0].init.body);
  assert.deepStrictEqual(Object.keys(body).sort(), ['text', 'userids']);
});

test('no bot or zapikey is a 503, and sends nothing', async () => {
  const r = recorder();
  for (const over of [{ botName: '' }, { zapikey: '' }]) {
    await assert.rejects(
      () => cliqWith(r.fetchImpl, over).dm('u1', 'hi'),
      (err) => err.status === 503 && /not connected to a Cliq bot/.test(err.message),
    );
  }
  assert.strictEqual(r.calls.length, 0, 'no request attempted without a credential');
});

test('a non-2xx throws with the status and the body', async () => {
  const r = recorder(400, '{"code":"extra_key_found"}');
  await assert.rejects(
    () => cliqWith(r.fetchImpl).dm('u1', 'hi'),
    (err) => err.status === 400 && err.message.includes('extra_key_found'),
  );
});

test('a 429 is raised, not retried', async () => {
  // Cliq's 50/min is shared across the deployment and a breach locks everyone
  // out for ten minutes, so the relay surfaces it instead of making it worse.
  const r = recorder(429, 'rate limit');
  await assert.rejects(() => cliqWith(r.fetchImpl).dm('u1', 'hi'),
                       (err) => err.status === 429);
  assert.strictEqual(r.calls.length, 1, 'exactly one attempt');
});

test('a non-JSON success body comes back as raw', async () => {
  const r = recorder(200, 'OK');
  assert.deepStrictEqual(await cliqWith(r.fetchImpl).dm('u1', 'hi'), { raw: 'OK' });
});

test('a trailing slash on the base URL does not double up', async () => {
  const r = recorder();
  await cliqWith(r.fetchImpl, { baseUrl: 'https://cliq.zoho.in/' }).dm('u1', 'hi');
  assert.strictEqual(r.calls[0].parsed.pathname, '/api/v2/bots/testbot/message');
});

test('dmFile targets the file endpoint, with user_id in the QUERY', async () => {
  // Unchanged by the v2 move, and pinned because the spelling differs between
  // the two endpoints in both name and position -- `user_id` in this query,
  // `userids` in the message body. That asymmetry is a standing trap.
  const r = recorder();
  await cliqWith(r.fetchImpl).dmFile('50000000001', {
    filename: 'plan.md', bytes: Buffer.from('x'), contentType: 'text/markdown',
    caption: 'the plan',
  });
  const { parsed, init } = r.calls[0];
  assert.strictEqual(parsed.pathname, '/api/v2/bots/testbot/files');
  assert.strictEqual(parsed.searchParams.get('user_id'), '50000000001');
  assert.match(init.headers['Content-Type'], /^multipart\/form-data; boundary=/);
  // comments must be a JSON ARRAY; a bare string is refused (measured).
  assert.ok(init.body.includes('["the plan"]'));
});
