'use strict';
/**
 * Relay logic, with no Catalyst or Cliq imports.
 *
 * Everything here works against a Store and a Cliq client passed in, so the
 * same code that runs in production is exercised by the local test harness.
 *
 * The rule that keeps users isolated from one another: the Cliq user is
 * always resolved from the bearer token server-side. No handler below ever
 * reads an identity out of a request body.
 */

const crypto = require('crypto');

const PAIR_CODE_TTL_MS = 10 * 60 * 1000;   // a code is useless after 10 minutes
const PENDING_TTL_MS = 24 * 60 * 60 * 1000;
const MAX_WAIT_S = 25;                      // Catalyst kills a function at 30s
// How often a held long-poll re-checks storage. Every tick is a billed
// read, and 3s of extra latency is imperceptible to someone who just
// typed a message, so this is the cheapest knob in the system.
const POLL_TICK_MS = Number(process.env.POLL_TICK_MS || 3000);
const MAX_TEXT = 4500;                      // Cliq caps a message at 5000

// Cliq's own ceiling is 50/min for bot messages, and it is shared across
// every user because we post under one token-owner identity. Leave headroom.
const SEND_BUDGET_PER_MIN = 40;

const sha256 = (s) => crypto.createHash('sha256').update(s).digest('hex');

function timingSafeEqual(a, b) {
  const ab = Buffer.from(String(a || ''));
  const bb = Buffer.from(String(b || ''));
  if (ab.length !== bb.length) return false;
  return crypto.timingSafeEqual(ab, bb);
}

function newToken() {
  return crypto.randomBytes(32).toString('hex');
}

function newPairCode() {
  return crypto.randomBytes(3).toString('hex');
}

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

/** Resolve the caller from the Authorization header alone. */
async function requireDevice(store, headers) {
  const auth = String(headers.authorization || headers.Authorization || '');
  if (!auth.startsWith('Bearer ')) throw new HttpError(401, 'missing bearer token');
  const device = await store.getDevice(sha256(auth.slice(7).trim()));
  if (!device) throw new HttpError(401, 'unknown or revoked device');
  return device;
}

function createRelay({ store, cliq, inboundSecret, debugUsers = [], now = Date.now }) {
  const debug = new Set(debugUsers.map((u) => String(u).toLowerCase()));

  const storesBody = (user) => debug.has(String(user || '').toLowerCase());

  async function withinSendBudget() {
    const bucket = `send:${Math.floor(now() / 60000)}`;
    const used = await store.bumpCounter(bucket, 120);
    return used <= SEND_BUDGET_PER_MIN;
  }

  return {
    /** Issue a pairing code. Called by the bot's welcome handler. */
    async issuePairCode({ cliqUser, email }) {
      if (!cliqUser || !email) throw new HttpError(400, 'cliqUser and email required');
      const code = newPairCode();
      await store.putPairCode(code, {
        cliq_user: String(cliqUser),
        email: String(email),
        expires: now() + PAIR_CODE_TTL_MS,
      });
      return { code, expires_in: PAIR_CODE_TTL_MS / 1000 };
    },

    /** POST /pair -- exchange a one-time code for a device token. */
    async pair({ body }) {
      const code = String(body.code || '').trim().toLowerCase();
      if (!code) throw new HttpError(400, 'code required');
      const rec = await store.getPairCode(code);
      if (!rec) throw new HttpError(403, 'unknown code');
      await store.deletePairCode(code);           // single use, even if expired
      if (rec.expires < now()) throw new HttpError(403, 'code expired');

      const token = newToken();
      await store.putDevice(sha256(token), {
        cliq_user: rec.cliq_user,
        email: rec.email,
        name: String(body.device_name || 'laptop').slice(0, 60),
        created: now(),
      });
      return { device_token: token, cliq_user: rec.email };
    },

    /** POST /unpair */
    async unpair({ headers }) {
      const auth = String(headers.authorization || '');
      if (auth.startsWith('Bearer ')) {
        await store.deleteDevice(sha256(auth.slice(7).trim()));
      }
      return { ok: true };
    },

    /**
     * POST /notify -- DM the paired user.
     * Note there is no recipient parameter; it comes from the token.
     */
    async notify({ headers, body }) {
      const device = await requireDevice(store, headers);
      const text = String(body.text || '').slice(0, MAX_TEXT);
      if (!text) throw new HttpError(400, 'text required');

      if (!(await withinSendBudget())) {
        // Breaching Cliq's limit costs a 10-minute lockout for everyone, so
        // shed the message rather than risk it.
        throw new HttpError(429, 'send budget exhausted; try again shortly');
      }

      const hint = String(body.hint || '').slice(0, 300);
      await cliq.dm(device.email, hint ? `${text}\n\n_${hint}_` : text);

      if (body.session_id) {
        await store.putSession(device.cliq_user, {
          session_id: String(body.session_id),
          cwd: String(body.cwd || ''),
          request_id: String(body.request_id || ''),
          ts: now(),
        });
      }
      return { ok: true, request_id: body.request_id || null };
    },

    /** POST /await -- long-poll for this device owner's replies. */
    async await_({ headers, body, sleep }) {
      const device = await requireDevice(store, headers);
      const since = Number(body.since || 0) || 0;
      const wait = Math.min(Number(body.wait) || MAX_WAIT_S, MAX_WAIT_S);
      const deadline = now() + wait * 1000;

      for (;;) {
        const msgs = await store.listPending(device.cliq_user, since);
        if (msgs.length) {
          // Pass whole rows: a composite-key store needs more than the id.
          await store.deletePending(msgs);
          return { messages: msgs, now: now() };
        }
        if (now() >= deadline) return { messages: [], now: now() };
        await sleep(POLL_TICK_MS);
      }
    },

    /** POST /cliq-inbound -- the bot's message handler delivering a reply. */
    async inbound({ headers, body }) {
      const given = headers['x-relay-secret'] || headers['X-Relay-Secret'];
      if (!inboundSecret || !timingSafeEqual(given, inboundSecret)) {
        throw new HttpError(403, 'bad secret');
      }
      const cliqUser = String(body.cliq_user || '').trim();
      const text = String(body.text || '').trim();
      if (!cliqUser || !text) throw new HttpError(400, 'cliq_user and text required');

      // Cliq does not redeliver, but a handler may fire twice on a retry of
      // its own; keying on the Cliq message id makes this idempotent.
      const id = String(body.message_id || crypto.randomUUID());
      await store.putPending({
        id,
        cliq_user: cliqUser,
        text: text.slice(0, MAX_TEXT),
        ts: now(),
        expires: now() + PENDING_TTL_MS,
        // Bodies are retained only for the debug allowlist; everyone else's
        // message is deleted the moment it is delivered.
        retain: storesBody(cliqUser),
      });
      return { ok: true };
    },
  };
}

module.exports = { createRelay, HttpError, sha256, newToken, newPairCode,
                   timingSafeEqual, SEND_BUDGET_PER_MIN, MAX_WAIT_S };
