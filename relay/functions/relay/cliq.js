'use strict';
/**
 * Outbound to Zoho Cliq.
 *
 * One webhook token for the whole deployment, owned by one account -- no
 * per-user OAuth anywhere. `/message` posts as the bot into the target
 * user's DM. (`/incoming` is a different thing entirely: it feeds params to
 * a Deluge handler and posts nothing by itself.)
 */

function createCliq({ baseUrl, botName, zapikey, fetchImpl = globalThis.fetch }) {
  const base = (baseUrl || 'https://cliq.zoho.com').replace(/\/$/, '');

  return {
    async dm(emailOrZuid, text) {
      // Checked here rather than at construction: the client is built for
      // every request, so throwing early would take down pairing and
      // long-polling too, which need no Cliq credential at all.
      if (!botName || !zapikey) {
        const err = new Error('relay is not connected to a Cliq bot yet '
                            + '(CLIQ_BOT_NAME / CLIQ_ZAPIKEY unset)');
        err.status = 503;
        throw err;
      }
      const url = `${base}/api/v2/bots/${encodeURIComponent(botName)}/message`
                + `?zapikey=${encodeURIComponent(zapikey)}`;
      const res = await fetchImpl(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text,
          userids: emailOrZuid,
          // Without this Cliq returns nothing useful; with it we get the
          // per-user chat_id and message_id back.
          sync_message: true,
        }),
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

module.exports = { createCliq };
