'use strict';
/**
 * Outbound to Zoho Cliq.
 *
 * One webhook token for the whole deployment, owned by one account -- no
 * per-user OAuth anywhere. `/messages` posts as the bot into the target
 * user's DM. (`/incoming` is a different thing entirely: it feeds params to
 * a Deluge handler and posts nothing by itself.)
 *
 * v3, not v2, for two reasons established by testing against the live API:
 * v3 renders Markdown in `text` while v2 shows it raw, and only v3 accepts
 * `slides` (native tables) and `buttons`.
 *
 * v3 differs from v2 in ways the docs do not lead with, each confirmed by a
 * 400 from the live endpoint:
 *   - `user_ids` is a QUERY parameter, not a body field
 *   - `sync_message` is not accepted at all, so no message_id comes back
 *   - `styles` is rejected outright, despite being in the published spec
 *   - unknown body keys are a hard error, so send only what is accepted
 */

const ALLOWED_KEYS = ['slides', 'buttons', 'card', 'bot'];

function createCliq({ baseUrl, botName, zapikey, fetchImpl = globalThis.fetch }) {
  const base = (baseUrl || 'https://cliq.zoho.com').replace(/\/$/, '');

  return {
    /**
     * DM a user as the bot.
     * `extras` may carry slides / buttons / card; anything else is dropped,
     * because v3 rejects the whole request on an unknown key.
     */
    async dm(emailOrZuid, text, extras = {}) {
      // Checked here rather than at construction: the client is built for
      // every request, so throwing early would take down pairing and
      // long-polling too, which need no Cliq credential at all.
      if (!botName || !zapikey) {
        const err = new Error('relay is not connected to a Cliq bot yet '
                            + '(CLIQ_BOT_NAME / CLIQ_ZAPIKEY unset)');
        err.status = 503;
        throw err;
      }

      const url = `${base}/api/v3/bots/${encodeURIComponent(botName)}/messages`
                + `?zapikey=${encodeURIComponent(zapikey)}`
                + `&user_ids=${encodeURIComponent(emailOrZuid)}`;

      const body = { text };
      for (const k of ALLOWED_KEYS) {
        if (extras[k] !== undefined && extras[k] !== null) body[k] = extras[k];
      }

      const res = await fetchImpl(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const raw = await res.text();
      if (!res.ok) {
        // 429 here means we tripped Cliq's 50/min and everyone is locked
        // out for 10 minutes. Surface it loudly rather than retrying.
        const err = new Error(`cliq ${res.status}: ${raw.slice(0, 300)}`);
        err.status = res.status;
        throw err;
      }
      try { return JSON.parse(raw); } catch { return { raw }; }
    },
  };
}

module.exports = { createCliq, ALLOWED_KEYS };
