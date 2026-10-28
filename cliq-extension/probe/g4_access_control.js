// G4: does Data Streams access control actually gate the CONNECTION, and does
// it fail open or closed? The relay's /ds-access refuses any connectionName
// beginning with "deny-". This ticket was minted for exactly such a name.
const fs = require('fs');
const SDK = require('path').join(__dirname, '/../../relay/functions/relay/node_modules/@zcatalyst/datastreams');
const { DataStreamsWebSocket } = require(SDK);
const r = JSON.parse(fs.readFileSync(process.env.TICKET, 'utf8')).steps.tokenPair.result;
const log = (...a) => console.log(new Date().toISOString().slice(11, 23), ...a);
const ws = new DataStreamsWebSocket({ url: r.url, key: r.key, zuid: r.wss_id });
let opened = false;
ws.on('open', () => { opened = true; log('OPEN — connection accepted'); ws.subscribe('0'); });
ws.on('message', (e) => log('message', e && e.streamingId));
ws.on('error', (e) => log('error', e && (e.message || JSON.stringify(e))));
ws.on('close', (e) => log('close code=', e && (e.code || JSON.stringify(e))));
setTimeout(() => {
  log('--- result ---');
  log(opened ? 'VERDICT: connection ACCEPTED despite /ds-access returning 403 -> access control either is not wired to this relay, or FAILS OPEN'
             : 'VERDICT: connection REFUSED -> Custom Logic is wired here and fails closed');
  try { ws.close(); } catch {}
  process.exit(0);
}, 20000);
