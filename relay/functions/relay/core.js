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
const { splitTables, splitForCliq, MAX_PART } = require('./format');
const askform = require('./askform');
const upload = require('./upload');
const { URL_TTL_SECONDS } = require('./stratus');

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
// INBOUND only -- a guard on what goes into a Pending row. Outbound text is
// no longer capped; it is split across messages instead (see notify).
const MAX_TEXT = 4500;

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
function buildButtons(requestId, answers, { fnName, fnOwner, askFnName }) {
  return answers.slice(0, 5).map((a) => ({
    // TWENTY, measured: Cliq rejects the whole message with
    // input_maxlength_reached on 'label' beyond that. The 100 here was our
    // own guess and let a 22-character label through, which cost a chooser.
    label: String(a.label || a.value).slice(0, 20),
    action: {
      type: 'invoke.function',
      // One answer can target a different function. The Answer button opens
      // a form, which is a different Deluge function from the one that just
      // records a tap -- and the laptop names neither, it only says what the
      // button is for.
      data: { name: a.value === 'form' ? askFnName : fnName,
              ...(fnOwner ? { owner: fnOwner } : {}) },
    },
    // The binding: the tap carries its own request id, so it cannot land on
    // the wrong session however many are outstanding.
    // A chooser button names its own request: the point of that message is
    // to pick between several, so one id for the whole message will not do.
    arguments: { req: String(a.req || requestId), ans: String(a.value) },
  }));
}

function createRelay({ store, cliq, inboundSecret, debugUsers = [], files = null, now = Date.now,
                       fnName = 'claudeCliqAction', fnOwner = '',
                       askFnName = 'claudeCliqAskForm',
                       askSubmitFnName = 'claudeCliqAskSubmit',
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
  /**
   * Ring the doorbell for one account, after the reply has been filed.
   *
   * Every path that queues a reply must call this. A tap used to queue a row
   * and ring nothing, so a button press sat in the relay until some unrelated
   * event woke the leader -- measured at four and a half minutes, and
   * indistinguishable from the button not working. That affected the chooser
   * and every permission Allow/Deny alike.
   *
   * Non-fatal on purpose: the reply is already filed, a laptop covers a missed
   * bell with one cheap fetch when it next starts waiting, and failing here
   * would tell Cliq delivery failed when it had not -- and Cliq does not
   * redeliver.
   */
  async function ringDoorbell(cliqUser) {
    const wake = wakeIdFor(cliqUser);
    if (!streams || !streams.configured || !wake) return;
    try {
      await streams.publishWake(wake);
    } catch (err) {
      console.error('wake publish failed (reply is still queued)', err && err.message);
    }
  }

  /**
   * Where one account's attachment for one message lives.
   *
   * Derived, never stored -- and the account is part of what is hashed. That
   * is the whole access control: `/file-url` derives the key from the
   * requesting DEVICE's account, so asking for someone else's message id
   * produces a key in the asker's own namespace, which holds nothing. No row
   * lookup is needed, which matters because the Pending row is deleted the
   * moment the reply is delivered -- before the laptop asks for the file.
   */
  function fileKeyFor(cliqUser, messageId) {
    if (!streamSecret || !cliqUser || !messageId) return '';
    const h = crypto.createHmac('sha256', streamSecret)
      .update(`file|${cliqUser}|${messageId}`).digest('hex').slice(0, 40);
    return `inbound/${h}`;
  }

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
      // NOT truncated. A long turn is sent as several messages instead -- the
      // end of a message is usually where the point is, and cutting it left no
      // way to ask for the rest.
      const text = String(body.text || '');
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

      // v3 renders Markdown in `text` but not tables, so lift those out into
      // native table slides and send the prose as-is.
      const { text: prose, slides } = splitTables(text);

      // The hint is appended to the LAST part, not to the text before
      // splitting: it carries the "(id xxxxx)" a swipe-reply binds to, so if
      // it rode along with the prose it would be the first thing lost when a
      // message overflowed -- breaking replies on exactly the longest ones.
      const hintBlock = hint ? `\n\n_${hint}_` : '';
      const budget = Math.max(500, MAX_PART - hintBlock.length - 80);
      const { parts, dropped } = splitForCliq(prose, { limit: budget });
      const pieces = parts.length ? parts : ['(see below)'];

      const extras = {};
      if (slides.length) extras.slides = slides;
      // Cliq allows at most five buttons on a message.
      if (Array.isArray(body.answers) && body.answers.length && body.request_id) {
        extras.buttons = buildButtons(String(body.request_id), body.answers,
                                      { fnName, fnOwner, askFnName });
      }

      // Sequentially, and in order: these are separate messages, and a reader
      // on a phone needs them to arrive the way they were written. Buttons,
      // tables and the hint go on the LAST one only, so a question is asked
      // once, at the end, where the reader has the whole picture.
      for (let i = 0; i < pieces.length; i += 1) {
        const last = i === pieces.length - 1;
        const marker = pieces.length > 1 ? `_(${i + 1}/${pieces.length})_\n\n` : '';
        const tail = last && dropped
          ? `\n\n_(${dropped} more characters did not fit — the rest is at the terminal.)_`
          : '';
        await cliq.dm(device.email, marker + pieces[i] + tail + (last ? hintBlock : ''),
                      last ? extras : {});
      }

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
      await ringDoorbell(cliqUser);
      return { ok: true, request_id: req, answer: ans };
    },

    /** POST /cliq-inbound -- the bot's message handler delivering a reply. */
    async inbound({ headers, body }) {
      const given = headers['x-relay-secret'] || headers['X-Relay-Secret'];
      if (!inboundSecret || !timingSafeEqual(given, inboundSecret)) {
        throw new HttpError(403, 'bad secret');
      }
      const cliqUser = String(body.cliq_user || '').trim();
      let text = String(body.text || '').trim();
      // An attachment's caption may be empty -- the file IS the message -- so
      // a name counts as content in the way text normally does.
      const fileName = upload.safeName(body.file_name || '', '');
      if (!cliqUser || (!text && !fileName)) {
        throw new HttpError(400, 'cliq_user and text required');
      }
      if (fileName) {
        // Set HERE rather than by the handler, so a caption that happens to
        // contain the marker cannot forge one: withMarker strips any existing
        // marker from the text before appending the real one.
        text = upload.withMarker(text, fileName);
      }

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

      await ringDoorbell(cliqUser);
      return { ok: true };
    },

    /**
     * POST /ask-form -- build the form behind a tapped Answer button.
     *
     * The questions come from the message text the tap hands back, not from
     * anything stored. A button's `arguments` cap at 300 characters and a
     * four-question spec is ~1.2KB -- but a button function is handed
     * `message.get("text")` exactly as posted, so the relay parses back output
     * it rendered itself and nothing is held between asking and answering.
     */
    async askForm({ headers, body }) {
      const given = headers['x-relay-secret'] || headers['X-Relay-Secret'];
      if (!inboundSecret || !timingSafeEqual(given, inboundSecret)) {
        throw new HttpError(403, 'bad secret');
      }
      const req = String(body.req || '').trim().toLowerCase();
      if (!/^[a-z]{5}$/.test(req)) throw new HttpError(400, 'bad request id');
      const questions = askform.parseQuestions(String(body.text || ''));
      if (!questions.length) {
        // Say so in the chat rather than opening an empty form, which would
        // read as the feature being broken.
        return { text: "I can't read that question any more — answer it at the terminal." };
      }
      return askform.buildForm(req, questions, { submitFn: askSubmitFnName });
    },

    /**
     * POST /ask-submit -- a filled or dismissed form.
     *
     * The form's own name IS the request id, so a submission is bound by
     * construction and cannot be mistaken for an answer to another question.
     */
    async askSubmit({ headers, body }) {
      const given = headers['x-relay-secret'] || headers['X-Relay-Secret'];
      if (!inboundSecret || !timingSafeEqual(given, inboundSecret)) {
        throw new HttpError(403, 'bad secret');
      }
      const cliqUser = String(body.cliq_user || '').trim();
      const req = String(body.name || '').trim().toLowerCase();
      if (!cliqUser || !/^[a-z]{5}$/.test(req)) {
        throw new HttpError(400, 'cliq_user and a valid form name required');
      }
      const cancelled = String(body.action || '') === 'cancel';

      if (!cancelled) {
        // The one validation that must happen here: Cliq accepts a form_error
        // only as the reply to a submission.
        const err = askform.missingOther(body.values);
        if (err) return err;
      }
      const picked = cancelled ? {} : askform.readSubmission(body.values);
      if (!cancelled && !Object.keys(picked).length) {
        const answered = Object.entries(body.values || {})
          .filter(([k, v]) => /^q\d+$/.test(k) && v != null && v !== '');
        if (answered.length) {
          // Something WAS chosen and none of it parsed, which means Cliq used
          // a shape we do not handle. Say so and log it: silently losing a
          // selection is how a multiSelect pick disappeared once already,
          // leaving only the typed Other text and no error anywhere.
          console.error('ask-submit: unreadable selection shape',
                        JSON.stringify(answered).slice(0, 400));
          return { type: 'form_error',
                   text: "I couldn't read that selection — this is a bug on my side, "
                         + 'not yours. Answer at the terminal and tell Claude.' };
        }
        return { type: 'form_error', text: 'Pick at least one answer.' };
      }

      await store.putPending({
        id: `ask-${req}-${now()}`,
        cliq_user: cliqUser,
        request_id: req,
        // A sentinel, so the collector treats this as a structured answer and
        // never regexes it for yes/no the way it would a typed reply.
        text: JSON.stringify({ ask: cancelled ? 'cancelled' : 'answered', picked }),
        ts: now(),
        expires: now() + PENDING_TTL_MS,
        retain: storesBody(cliqUser),
      });
      await ringDoorbell(cliqUser);
      // Cancelling is not nothing: the form is built with trigger_on_cancel
      // so a dismissal reaches us and hands the session straight back to the
      // terminal, rather than leaving it held for the rest of its window.
      // Worth saying outright -- "Left it for the terminal." was asked about,
      // which means it explained neither what happened nor what to do next.
      return {
        text: cancelled
          ? 'Cancelled — nothing sent. The question is waiting at your terminal.'
          : '✓ Sent',
      };
    },

    /**
     * POST /stream-ticket -- what a laptop needs to hear its own doorbell.
     *
     * The ticket is the real security boundary in this design: a subscriber
     * cannot connect without one, and only this route mints them, only for a
     * device presenting a valid device token. Channel access control adds
     * nothing on top -- measured, see docs/FINDINGS.md.
     */
    /**
     * POST /cliq-file -- the bot's handler uploading an attachment's bytes.
     *
     * The bytes arrive from Deluge's `invokeurl ... files:`, which is the only
     * durable way to get them: a Cliq attachment URL is transient and expires
     * within minutes, and the laptop holds no credential that could read one.
     *
     * The reply text goes through /cliq-inbound as it always has -- this is a
     * separate addendum bound to the same message id, so the proven text path
     * is untouched and a failed upload costs the picture, never the reply.
     */
    async cliqFile({ headers, query, body }) {
      const given = headers['x-relay-secret'] || headers['X-Relay-Secret'];
      if (!inboundSecret || !timingSafeEqual(given, inboundSecret)) {
        throw new HttpError(403, 'bad secret');
      }
      if (!files || !files.configured) throw new HttpError(503, 'no bucket configured');

      const cliqUser = String((query && query.u) || '').trim();
      const messageId = String((query && query.mid) || '').trim();
      if (!cliqUser || !messageId) throw new HttpError(400, 'u and mid required');

      const key = fileKeyFor(cliqUser, messageId);
      if (!key) throw new HttpError(503, 'no stream secret, so no file key');

      const got = upload.unwrapUpload(headers['content-type'], body);
      if (!got.bytes.length) throw new HttpError(400, 'no file in the upload');

      await files.put(key, got.bytes);
      // How Deluge frames `files:` is undocumented, and this is the only place
      // it is visible -- the handler discards whatever it gets back. Logged
      // (shape only, never bytes or a key) so the answer is recorded rather
      // than inferred again.
      console.log(`cliq-file: framing=${got.framing} bytes=${got.bytes.length} `
                  + `kind=${upload.sniff(got.bytes) || 'unknown'} `
                  + `declared=${JSON.stringify(headers['content-type'] || '')}`);
      // `framing` and `sniff` are returned because the handler logs nothing
      // and this is the only place the shape Deluge actually sends is visible.
      return { ok: true, bytes: got.bytes.length, framing: got.framing,
               kind: upload.sniff(got.bytes) || got.contentType || '' };
    },

    /**
     * POST /file-url -- the laptop collecting an attachment.
     *
     * The key is derived from the DEVICE's own account, so `message_id` is
     * untrusted input that can only ever name something in the caller's own
     * namespace. `done` deletes it once the download has succeeded.
     */
    async fileUrl({ headers, body }) {
      const device = await requireDevice(store, headers);
      if (!files || !files.configured) throw new HttpError(503, 'no bucket configured');

      const messageId = String((body && body.message_id) || '').trim();
      if (!messageId) throw new HttpError(400, 'message_id required');
      const key = fileKeyFor(device.cliq_user, messageId);
      if (!key) throw new HttpError(503, 'no stream secret, so no file key');

      if (body && body.done) {
        // Stratus schedules the deletion rather than performing it, so this
        // reports what it promised, not that the object has gone.
        const res = await files.remove(key);
        return { ok: true, ...res };
      }
      const url = await files.signedGet(key);
      if (!url) throw new HttpError(404, 'no file for that message');
      return { url, expires_in: Number(URL_TTL_SECONDS) };
    },

    /** POST /files-check -- does the bucket exist? Saves a console trip. */
    async filesCheck({ headers }) {
      const given = headers['x-relay-secret'] || headers['X-Relay-Secret'];
      if (!inboundSecret || !timingSafeEqual(given, inboundSecret)) {
        throw new HttpError(403, 'bad secret');
      }
      if (!files || !files.configured) return { configured: false };
      return { configured: true, bucket: files.bucketName, ...(await files.check()) };
    },

    async streamTicket({ headers }) {
      const device = await requireDevice(store, headers);
      if (!streams || !streams.configured) {
        throw new HttpError(503, 'no Data Streams channel configured');
      }
      // RANDOM per ticket, not derived from the device.
      //
      // A per-device name was the obvious choice and it is wrong: a
      // connection name is a LEASE, not a label. Minting a second ticket for
      // a name whose subscriber session still exists is refused with
      // "Duplicate value for connection_name", and close() preserves that
      // session on purpose. So the first blocked hook got the stream and
      // every hook after it fell back to polling -- silently, because the
      // fallback works.
      //
      // Nothing needs the name to be stable: it identifies a connection, and
      // the ticket is what authorises. Random is also strictly better for
      // privacy, since a stable name handed to access control in the clear
      // would have re-linked devices regardless of the rotating wake label.
      //
      // TWENTY characters, measured: a longer connection_name is refused
      // with "Please check if the length of the input is in the correct
      // range", which names neither the field nor the limit. 21 fails.
      const conn = crypto.randomBytes(10).toString('hex');
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
