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
    debugUsers: (process.env.DEBUG_USERS || '').split(',').map((s) => s.trim()).filter(Boolean),
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
app.post('/await',  route((r, req) => r.await_({ headers: req.headers, body: req.body, sleep })));

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

app.get('/health', (_req, res) => res.status(200).json({ ok: true }));

module.exports = app;
