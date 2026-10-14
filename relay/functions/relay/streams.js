'use strict';
/**
 * Catalyst Data Streams -- the doorbell.
 *
 * Two jobs only: ring a bell, and mint a ticket so a laptop can hear bells.
 * **No message body ever passes through here.** A channel broadcasts to every
 * subscriber in the org, so the reply itself stays in the Pending table and
 * this carries an opaque label and nothing else.
 *
 * Why the package is vendored
 * ---------------------------
 * `@zcatalyst/datastreams` is documented but NOT published -- `npm install`
 * returns a 404 from the registry, and neither zcatalyst-sdk-node@3.4.0, the
 * PyPI zcatalyst-sdk, nor Maven carries a datastreams module. vendor/ holds a
 * copy built from github:catalystbyzoho/zcatalyst-sdk-js with the pnpm
 * `workspace:*` dependencies swapped for the published @zcatalyst/utils and
 * @zcatalyst/transport. Rebuild steps are in docs/FINDINGS.md.
 *
 * Why the app is initialised from the request
 * -------------------------------------------
 * `new DataStreams()` with no argument is what the docs show, and it cannot
 * work. @zcatalyst/auth-admin@1.0.0 loads credentials from the environment
 * through
 *
 *     if (!isNonEmptyString(!projectId)) return {};
 *
 * and `!projectId` is a boolean, never a string, so isNonEmptyString is always
 * false, the guard always fires, and getDefaultCredentials() always throws
 * AUTH_ERROR. Beyond the inverted condition it is the wrong idea for Advanced
 * I/O anyway: x-zc-projectid and friends arrive as request HEADERS, not as
 * process env. So the app is built per request, exactly as the old SDK's
 * catalyst.initialize(req) does it.
 */

const { DataStreams } = require('@zcatalyst/datastreams');
const { ZCAuth } = require('@zcatalyst/auth-admin');

function createStreams({ req, channelId }) {
  // Built lazily: most requests never ring a bell, and a relay whose Cliq
  // path still works should not fall over because the streams SDK cannot
  // initialise.
  let client = null;
  const sdk = () => {
    if (!client) client = new DataStreams(new ZCAuth().init(req));
    return client;
  };

  const need = () => {
    if (!channelId) {
      const err = new Error('DS_CHANNEL_ID is unset; no channel to publish to');
      err.status = 503;
      throw err;
    }
    return channelId;
  };

  return {
    get channelId() { return channelId || ''; },
    get configured() { return Boolean(channelId); },

    /**
     * Ring the bell for one Cliq account.
     *
     * `wakeId` is an opaque per-account label. Everyone subscribed hears it;
     * all it tells a listener who is not its owner is that *some* account had
     * activity. Keep it that way -- nothing identifying belongs in here.
     */
    async publishWake(wakeId) {
      return sdk().publishData(need(), JSON.stringify({ w: String(wakeId) }));
    },

    /** Mint a subscriber ticket: {url, key, wss_id, channel_id}. */
    async tokenPair(connectionName) {
      return sdk().getTokenPair(need(), { connectionName: String(connectionName) });
    },

    /** Diagnostic only: proves the SDK can reach Catalyst at all. */
    async channels() {
      return sdk().getAllChannels();
    },

    /** Diagnostic: what access control a channel actually has configured. */
    async details(id) {
      return sdk().getChannelDetails(String(id));
    },
  };
}

module.exports = { createStreams };
