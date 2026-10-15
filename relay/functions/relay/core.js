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
const { splitTables } = require('./format');

// Outbound messages end with "(id xxxxx)", so a swipe-reply hands that text
// straight back in replied_message.text. Pulling the id out of it binds the
// reply to its request with nothing typed and nothing stored.
// Tolerant of the markdown around it: the hint writes the id in backticks,
// and replied_message.text hands back the raw source, decoration included.
const REPLIED_ID = /\(id[\s:*_`]*([a-z]{5})[\s*_`]*\)/i;

const PAIR_CODE_TTL_MS = 10 * 60 * 1000;   // a code is useless after 10 minutes
const PENDING_TTL_MS = 24 * 60 * 60 * 1000;
const MAX_WAIT_S = 25;                      // Catalyst kills a function at 30s
// How often a held long-poll re-checks storage. Every tick is a billed
// read, and 3s of extra latency is imperceptible to someone who just
// typed a message, so this is the cheapest knob in the system.
const POLL_TICK_MS = Number(process.env.POLL_TICK_MS || 3000);
const MAX_TEXT = 4500;                      // Cliq caps a message at 5000

// Cliq's own ceiling is 50/min for bot messages, and it is shared across

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

/**
 * Resolve the caller from the device-token header alone.
 *
 * Deliberately not `Authorization`: Catalyst's gateway claims that header
 * and rejects anything that isn't one of its own OAuth tokens, so the
 * request never reaches this function.
 */
const DEVICE_HEADER = 'x-device-token';

async function requireDevice(store, headers) {
  const token = String(headers[DEVICE_HEADER] || headers['X-Device-Token'] || '').trim();
  if (!token) throw new HttpError(401, 'missing device token');
  const device = await store.getDevice(sha256(token));
  if (!device) throw new HttpError(401, 'unknown or revoked device');
  return device;
}

/**
 * Turn the semantic answers a hook offers into Cliq buttons.
 *
 * The laptop sends {label, value}; the Cliq shape is built here so the hooks
 * stay free of it. `owner` matters for distribution: it is optional only for
 * the person who created the function, and required for everyone else.
 */
function buildButtons(requestId, answers, { fnName, fnOwner }) {
  return answers.slice(0, 5).map((a) => ({
    label: String(a.label || a.value).slice(0, 100),
    action: {
      type: 'invoke.function',
      data: { name: fnName, ...(fnOwner ? { owner: fnOwner } : {}) },
    },
    // The binding: the tap carries its own request id, so it cannot land on
    // the wrong session however many are outstanding.
    // A chooser button names its own request: the point of that message is
    // to pick between several, so one id for the whole message will not do.
    arguments: { req: String(a.req || requestId), ans: String(a.value) },
  }));
}

function createRelay({ store, cliq, inboundSecret, debugUsers = [], now = Date.now,
                       fnName = 'claudeCliqAction', fnOwner = '',
                       streams = null, streamSecret = '' }) {
  const debug = new Set(debugUsers.map((u) => String(u).toLowerCase()));

  /**
   * The opaque label the doorbell rings for one Cliq account.
   *
   * Per ACCOUNT, not per device, because the mailbox is per account:
   * listPending/deletePending are keyed on cliq_user and a fetch drains
   * everything that account has queued. A per-device label would also need a
   * device-listing query that does not exist, one read per inbound reply, and
   * N publishes -- and it would still race, since the fetch is not
   * device-scoped.
   *
   * Derived rather than stored, so there is no column to add, no UPDATE per
   * rotation (the UPDATE tier is a tenth of the read tier), and nothing to
   * migrate for devices that are already paired.
   *
   * Everyone subscribed to the channel sees every label. All it tells someone
   * who does not own it is that *an* account had activity, and when. Nothing
   * identifying may ever be put alongside it.
   */
  function wakeIdFor(cliqUser) {
    if (!streamSecret) return '';
    return crypto.createHmac('sha256', streamSecret)
      .update(`wake|${cliqUser}`).digest('hex').slice(0, 32);
  }

  const storesBody = (user) => debug.has(String(user || '').toLowerCase());

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
      const token = String(headers[DEVICE_HEADER] || headers['X-Device-Token'] || '').trim();
      if (token) await store.deleteDevice(sha256(token));
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

      // A per-send counter used to sit here guarding Cliq's shared 50/min.
      // It cost a SELECT and an UPDATE on EVERY message -- and the UPDATE
      // free tier is 1,000/month against 10,000 for reads, so two writes per
      // notification blew the write tier on their own, before any polling.
      // There is nowhere cheap to move it: Catalyst's Cache has no atomic
      // increment, and get+put there lands on a 1,000/month GET tier, which
      // is worse still.
      //
      // So it is gone, and the gap is written down rather than approximated.
      // A breach now surfaces as Cliq's own 429, which cliq.dm detects and
      // raises. Pacing is enforced per laptop instead, which bounds the
      // realistic failure -- one runaway machine -- but NOT the real scaling
      // risk of fifty people active at once. See docs/FINDINGS.md.
      const hint = String(body.hint || '').slice(0, 300);
      const composed = hint ? `${text}\n\n_${hint}_` : text;

      // v3 renders Markdown in `text` but not tables, so lift those out into
      // native table slides and send the prose as-is.
      const { text: prose, slides } = splitTables(composed);
      const extras = {};
      if (slides.length) extras.slides = slides;
      // Cliq allows at most five buttons on a message.
      if (Array.isArray(body.answers) && body.answers.length && body.request_id) {
        extras.buttons = buildButtons(String(body.request_id), body.answers,
                                      { fnName, fnOwner });
      }
      await cliq.dm(device.email, prose || '(see below)', extras);

      // The Sessions row that used to be written here is gone with the
      // daemon. It was a SELECT plus an UPDATE on every notification and
      // nothing ever read it back: its only consumer was the daemon's
      // handle_unmatched, deciding whether a reply with nothing waiting
      // belonged to a live session. A blocked hook knows that for itself.
      return { ok: true, request_id: body.request_id || null };
    },

    /**
     * POST /peek -- what is queued, without consuming it.
     *
     * Exists because diagnosing with /await destroys the evidence: the
     * reply you are investigating is deleted by the act of looking.
     */
    async peek({ headers }) {
      const device = await requireDevice(store, headers);
      const msgs = await store.listPending(device.cliq_user, 0);
      return { messages: msgs, now: now() };
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

    /**
     * POST /cliq-action -- a tapped button, relayed by the bot's Deluge
     * function.
     *
     * Deliberately lands in the same pending queue a typed reply uses, so
     * there is one delivery path rather than two. The tap carries the
     * request id, which is what makes it unambiguous.
     *
     * Note a tap can only ever reach the tapper's own devices: the row is
     * keyed by the Cliq user the bot invoked the function for, so a forged
     * request id cannot reach anybody else's session.
     */
    async action({ headers, body }) {
      const given = headers['x-relay-secret'] || headers['X-Relay-Secret'];
      if (!inboundSecret || !timingSafeEqual(given, inboundSecret)) {
        throw new HttpError(403, 'bad secret');
      }
      const cliqUser = String(body.cliq_user || '').trim();
      const req = String(body.req || '').trim().toLowerCase();
      if (!cliqUser || !req) throw new HttpError(400, 'cliq_user and req required');

      // An empty answer is the chooser: the laptop already holds the text and
      // only needs to be told which request it belongs to.
      const ANSWERS = { allow: 'yes', deny: 'no', all: 'all', pick: '' };
      const ans = String(body.ans || 'pick').trim().toLowerCase();
      if (!(ans in ANSWERS)) throw new HttpError(400, `unknown answer: ${ans}`);

      await store.putPending({
        id: `tap-${req}-${now()}`,
        cliq_user: cliqUser,
        request_id: req,
        text: ANSWERS[ans],
        ts: now(),
        expires: now() + PENDING_TTL_MS,
        retain: storesBody(cliqUser),
      });
      return { ok: true, request_id: req, answer: ans };
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
      const replied = String(body.replied_text || '');
      const m = REPLIED_ID.exec(replied);
      await store.putPending({
        id,
        cliq_user: cliqUser,
        // Bound when the user replied TO a specific message; empty when they
        // just typed into the chat.
        request_id: m ? m[1].toLowerCase() : '',
        text: text.slice(0, MAX_TEXT),
        ts: now(),
        expires: now() + PENDING_TTL_MS,
        // Bodies are retained only for the debug allowlist; everyone else's
        // message is deleted the moment it is delivered.
        retain: storesBody(cliqUser),
      });

      // Ring the doorbell. Deliberately after the insert and deliberately
      // non-fatal: the reply is already safely filed, and a laptop covers a
      // missed bell with one cheap fetch when it next starts waiting. Failing
      // the inbound here would make Cliq think delivery failed when it did
      // not -- and Cliq does not redeliver.
      const wake = wakeIdFor(cliqUser);
      if (streams && streams.configured && wake) {
        try {
          await streams.publishWake(wake);
        } catch (err) {
          console.error('wake publish failed (reply is still queued)', err && err.message);
        }
      }
      return { ok: true };
    },

    /**
     * POST /stream-ticket -- what a laptop needs to hear its own doorbell.
     *
     * The ticket is the real security boundary in this design: a subscriber
     * cannot connect without one, and only this route mints them, only for a
     * device presenting a valid device token. Channel access control adds
     * nothing on top -- measured, see docs/FINDINGS.md.
     */
    async streamTicket({ headers }) {
      const device = await requireDevice(store, headers);
      if (!streams || !streams.configured) {
        throw new HttpError(503, 'no Data Streams channel configured');
      }
      // Derived from the token hash so it is stable per device and reveals
      // nothing: the hash is already one-way, and this is one more step from
      // it. Used only to name the connection, never to authorise anything.
      //
      // TWENTY characters, measured: Data Streams rejects a longer
      // connection_name with "Please check if the length of the input is in
      // the correct range", which names neither the field nor the limit. 21
      // fails, 20 passes. 80 bits of SHA-256 is ample to keep devices apart.
      const conn = sha256(`conn|${device.token_hash}`).slice(0, 20);
      const tp = await streams.tokenPair(conn);
      return {
        url: tp && tp.url,
        key: tp && tp.key,
        wss_id: tp && (tp.wss_id || tp['wss-id']),
        // From our own config, not from the response: the docs list
        // channel_id as a token-pair field and the live API does not return
        // it. The laptop only logs this, but a silent undefined is the kind
        // of thing that gets debugged twice.
        channel_id: streams.channelId,
        wake_id: wakeIdFor(device.cliq_user),
      };
    },
  };
}

module.exports = { createRelay, buildButtons, HttpError, sha256, newToken, newPairCode,
                   DEVICE_HEADER,
                   timingSafeEqual, MAX_WAIT_S };
