#!/usr/bin/env node
/**
 * G3: is Data Streams ack-gating per-subscriber or per-channel?
 *
 * The docs say only "until the acknowledgement is sent, the next message in
 * the stream will not be delivered". If that gating is per CHANNEL, then one
 * laptop that hears a bell and never acknowledges it wedges the doorbell for
 * the whole org, and a single shared channel is unusable.
 *
 * Subscriber A connects and NEVER acks. Subscriber B connects and acks every
 * message. Then three wakes are published.
 *
 *   B receives all three  -> gating is per-subscriber. Design survives.
 *   B stops after one     -> gating is per-channel. Design collapses.
 */

const fs = require('fs');
const path = require('path');

// Resolved absolutely: this script lives outside the relay, and node looks
// for modules next to the SCRIPT, not next to the working directory.
const SDK = '/Users/nalin-20844/claude-cliq/relay/functions/relay/node_modules/@zcatalyst/datastreams';
const { DataStreamsWebSocket } = require(SDK);

const DIR = process.env.TICKET_DIR;
const ticket = (n) => {
  const d = JSON.parse(fs.readFileSync(path.join(DIR, `ticket-${n}.json`), 'utf8'));
  const r = d.steps.tokenPair.result;
  return { url: r.url, key: r.key, zuid: r.wss_id };
};

const log = (...a) => console.log(new Date().toISOString().slice(11, 23), ...a);

function subscriber(name, { ack }) {
  const t = ticket(name);
  const seen = [];
  const ws = new DataStreamsWebSocket({ url: t.url, key: t.key, zuid: t.zuid });

  ws.on('open', () => {
    log(`${name}: open -> subscribe('0')`);
    ws.subscribe('0');
  });
  ws.on('message', (ev) => {
    const id = ev && ev.streamingId;
    seen.push(id);
    log(`${name}: MESSAGE #${seen.length} streamingId=${id} data=${JSON.stringify(ev && ev.data)}`);
    if (ack) {
      ws.sendAck();
      log(`${name}: acked`);
    } else {
      log(`${name}: deliberately NOT acking`);
    }
  });
  ws.on('error', (e) => log(`${name}: error`, e && (e.message || e)));
  ws.on('close', (e) => log(`${name}: close`, e && (e.code || '')));

  return { ws, seen, name };
}

(async () => {
  // A first and given a head start, so it is definitely the one holding an
  // unacknowledged message when B's messages start arriving.
  const A = subscriber('A', { ack: false });
  await new Promise((r) => setTimeout(r, 4000));
  const B = subscriber('B', { ack: true });

  await new Promise((r) => setTimeout(r, Number(process.env.RUN_MS || 45000)));

  log('--- result ---');
  log(`A (never acks) received ${A.seen.length}`);
  log(`B (acks each)  received ${B.seen.length}`);
  const verdict = B.seen.length >= 3
    ? 'PER-SUBSCRIBER — a stalled subscriber does not block others. Design survives.'
    : B.seen.length <= 1
      ? 'PER-CHANNEL or stalled — B did not get the later wakes. Investigate before building.'
      : 'INCONCLUSIVE — B got some but not all; re-run with more spacing.';
  log('VERDICT:', verdict);

  try { A.ws.close(); B.ws.close(); } catch {}
  process.exit(0);
})();
