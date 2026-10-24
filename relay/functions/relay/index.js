'use strict';
/**
 * Catalyst Advanced I/O entry point.
 *
 * Advanced I/O is public by default (authentication: optional), which is
 * what lets Cliq POST in with no Catalyst credential. Our own checks are
 * the bearer device token on the laptop routes and a shared secret on the
 * two routes Cliq calls.
 */

const express = require('express');
const catalyst = require('zcatalyst-sdk-node');

const { createRelay } = require('./core');
const { createStore } = require('./store-datastore');
const { createCliq } = require('./cliq');
const { runSelftest } = require('./selftest');
const { createStreams } = require('./streams');
const { createFiles, MAX_BYTES } = require('./stratus');

const app = express();
app.use(express.json({ limit: '256kb' }));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function relayFor(req) {
  const capp = catalyst.initialize(req);
  return createRelay({
    store: createStore(capp),
    cliq: createCliq({
      baseUrl: process.env.CLIQ_BASE_URL,
      botName: process.env.CLIQ_BOT_NAME,
      zapikey: process.env.CLIQ_ZAPIKEY,
    }),
    inboundSecret: process.env.RELAY_INBOUND_SECRET,
    fnName: process.env.CLIQ_FUNCTION_NAME || 'claudeCliqAction',
    // Optional for the function's creator, required for everyone else.
    fnOwner: process.env.CLIQ_FUNCTION_OWNER || '',
    askFnName: process.env.CLIQ_ASK_FUNCTION_NAME || 'claudeCliqAskForm',
    // The form names its own submit handler, so this has to reach buildForm.
    // Defaulting it inside askform.js was not enough: the default is a name
    // that only exists in an untouched install, and Cliq answers a submission
    // to a missing function with "This function does not exist".
    askSubmitFnName: process.env.CLIQ_ASK_SUBMIT_FUNCTION_NAME || 'claudeCliqAskSubmit',
    debugUsers: (process.env.DEBUG_USERS || '').split(',').map((s) => s.trim()).filter(Boolean),
    // Built per request like everything else -- Data Streams needs an app
    // initialised from the request headers, not from process env.
    streams: createStreams({ req, channelId: process.env.DS_CHANNEL_ID || '' }),
    // Derives each account's wake label. Not a credential a client ever
    // holds -- it only has to be stable and secret to the relay.
    streamSecret: process.env.RELAY_STREAM_SECRET || '',
    // Attachment storage. Absent STRATUS_BUCKET the file routes answer 503
    // and everything else carries on exactly as before.
    files: createFiles({ req, bucketName: process.env.STRATUS_BUCKET || '' }),
  });
}

const route = (fn) => async (req, res) => {
  try {
    res.status(200).json(await fn(relayFor(req), req));
  } catch (err) {
    const status = err.status || 500;
    if (status >= 500) console.error('relay error', err);
    res.status(status).json({ error: err.message || 'error' });
  }
};

function requireSecret(req) {
  const given = req.headers['x-relay-secret'];
  const want = process.env.RELAY_INBOUND_SECRET;
  if (!want || given !== want) {
    const e = new Error('bad secret'); e.status = 403; throw e;
  }
}

// -- laptop-facing ---------------------------------------------------------
app.post('/pair',   route((r, req) => r.pair({ body: req.body })));
app.post('/unpair', route((r, req) => r.unpair({ headers: req.headers })));
app.post('/notify', route((r, req) => r.notify({ headers: req.headers, body: req.body })));
app.post('/peek',   route((r, req) => r.peek({ headers: req.headers })));
app.post('/await',  route((r, req) => r.await_({ headers: req.headers, body: req.body, sleep })));
app.post('/stream-ticket', route((r, req) => r.streamTicket({ headers: req.headers })));
app.post('/file-url',    route((r, req) => r.fileUrl({ headers: req.headers, body: req.body })));
// A document for the paired user's DM. Raw like /cliq-file: the global
// express.json is 256kb and is not for bytes. Metadata rides in the query so
// the body stays exactly the file.
app.post('/notify-file',
  express.raw({ type: '*/*', limit: MAX_BYTES }),
  route((r, req) => r.notifyFile({ headers: req.headers, query: req.query, body: req.body })));
app.post('/files-check', route((r, req) => r.filesCheck({ headers: req.headers })));
// The ONE route that is not JSON: Deluge uploads an attachment's bytes here.
// express.json() above leaves a non-JSON content type alone, so the body is
// still unread by the time this raw parser sees it. `type: '*/*'` because what
// Deluge sends is the thing being discovered -- unwrapUpload handles either.
app.post('/cliq-file',
  express.raw({ type: '*/*', limit: MAX_BYTES }),
  route((r, req) => r.cliqFile({ headers: req.headers, query: req.query, body: req.body })));
// Both are called by the bot's Deluge functions, secret-gated inside core.
app.post('/ask-form',   route((r, req) => r.askForm({ headers: req.headers, body: req.body })));
app.post('/ask-submit', route((r, req) => r.askSubmit({ headers: req.headers, body: req.body })));

// -- Cliq-facing -----------------------------------------------------------
app.post('/cliq-action', route((r, req) =>
  r.action({ headers: req.headers, body: req.body })));

app.post('/cliq-inbound', (req, res, next) => {
  // Logged before the secret check so a handler calling with the wrong
  // secret is still visible in the Catalyst logs. Never log the secret or
  // the message body.
  console.log('[inbound] hit', JSON.stringify({
    has_secret: Boolean(req.headers['x-relay-secret']),
    body_keys: Object.keys(req.body || {}),
    cliq_user: (req.body || {}).cliq_user || null,
    text_len: String((req.body || {}).text || '').length,
    ua: req.headers['user-agent'] || null,
  }));
  next();
}, route((r, req) => r.inbound({ headers: req.headers, body: req.body })));

// Called by the bot's welcome_handler the moment someone subscribes.
app.post('/issue-code', route((r, req) => {
  requireSecret(req);
  return r.issuePairCode({ cliqUser: req.body.cliq_user, email: req.body.email });
}));

// Verifies every table round-trips. Secret-gated: it writes rows.
app.post('/selftest', async (req, res) => {
  try {
    requireSecret(req);
    res.status(200).json(await runSelftest(catalyst.initialize(req)));
  } catch (err) {
    res.status(err.status || 500).json({ error: err.message });
  }
});

/**
 * POST /ds-probe -- the Data Streams gate, in one call.
 *
 * Reports each step separately rather than failing on the first, because the
 * interesting outcomes are "the SDK loaded but Catalyst refused" and "channels
 * list but a ticket will not mint" -- and those mean completely different
 * things for the design.
 *
 * Pass {"channel_id": "..."} to override DS_CHANNEL_ID before it is deployed.
 */
app.post('/ds-probe', async (req, res) => {
  const out = { steps: {} };
  const step = async (name, fn) => {
    try {
      out.steps[name] = { ok: true, result: await fn() };
    } catch (err) {
      out.steps[name] = {
        ok: false,
        error: err && err.message ? err.message : String(err),
        code: err && err.code ? err.code : undefined,
        status: err && err.status ? err.status : undefined,
      };
    }
  };
  try {
    requireSecret(req);
    const channelId = String((req.body && req.body.channel_id) || process.env.DS_CHANNEL_ID || '');
    const streams = createStreams({ req, channelId });
    out.channel_id = channelId || '(unset)';

    // G0: does the vendored SDK load, initialise and reach Catalyst at all?
    await step('load', async () => {
      require('@zcatalyst/datastreams');
      return 'vendored package resolved';
    });
    await step('channels', () => streams.channels());
    if (channelId) await step('details', () => streams.details(channelId));

    // G1: can a server-side function mint a subscriber ticket? The docs say
    // this needs admin auth; the SDK asks with CREDENTIAL_USER.user, and this
    // function has no end user. If it fails, the push design is dead.
    if (channelId) {
      await step('tokenPair', async () => {
        const name = String((req.body && req.body.conn) || 'probe-' + Date.now());
        const tp = await streams.tokenPair(name);
        // The key is NEVER returned here. A `reveal: true` flag used to hand
        // out a usable listening pass, because the gate tests needed real
        // tickets before /stream-ticket existed. It does now, properly, for a
        // device that presents a valid device token -- so this is a door whose
        // only remaining job would be handing out credentials.
        return {
          url: tp && tp.url,
          wss_id: tp && (tp.wss_id || tp['wss-id']),
          channel_id: tp && tp.channel_id,
          key_present: Boolean(tp && tp.key),
        };
      });
      await step('publish', () => streams.publishWake('probe'));
    }
    res.status(200).json(out);
  } catch (err) {
    res.status(err.status || 500).json({ error: err.message || 'error', ...out });
  }
});

/**
 * POST /ds-access -- Data Streams "Custom Logic" access control.
 *
 * Called by Catalyst when a subscriber tries to connect, with
 * {userId, connectionName} in the body. 200 {approved:true} lets them in,
 * 403 {approved:false, reason} keeps them out.
 *
 * Be honest about what this is: **it is not the security boundary.** A
 * subscriber cannot connect without a token pair, token pairs are minted only
 * by /stream-ticket after a valid device token is presented, and minting needs
 * a credential no client holds. This handler exists because the console
 * requires one to be named, and because a shape check is cheap. It
 * deliberately does no Data Store lookup -- a read here would put a billed
 * query on every reconnect, and it would buy nothing the ticket did not
 * already prove.
 */
function dsAccess(req, res) {
  const connectionName = String((req.body && req.body.connectionName) || '');
  const userId = String((req.body && req.body.userId) || '');
  // Logged so the gate test can tell "approved" from "never called at all",
  // which are the two outcomes that look identical from the client side.
  console.log('ds-access called', JSON.stringify({ connectionName, userId }));
  const ok = /^[A-Za-z0-9_.:-]{3,64}$/.test(connectionName) || /^[0-9]{3,32}$/.test(userId);
  if (!ok) {
    return res.status(403).json({ approved: false, reason: 'Unauthorized' });
  }
  return res.status(200).json({ approved: true });
}

// Mounted at BOTH paths on purpose. Catalyst invokes the FUNCTION for access
// control, which arrives at the express root -- it has no way to know about a
// sub-path. /ds-access stays as the self-documenting name and for direct
// testing.
app.post('/ds-access', dsAccess);
app.post('/', dsAccess);

app.get('/health', (_req, res) => res.status(200).json({ ok: true }));

// Express answers an oversized body with an HTML error page, which the Deluge
// handler cannot read -- so it treated a 413 as success, claimed a file had
// been forwarded, and left the laptop to 404 on an object that was never
// stored. JSON, with the limit named, so the bot can say what actually went
// wrong.
app.use((err, req, res, next) => {
  if (err && (err.type === 'entity.too.large' || err.status === 413)) {
    return res.status(413).json({
      error: 'file too large',
      limit_mb: Math.round(MAX_BYTES / (1024 * 1024)),
    });
  }
  return next(err);
});

module.exports = app;
