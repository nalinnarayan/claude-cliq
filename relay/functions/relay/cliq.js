'use strict';
/**
 * Outbound to Zoho Cliq.
 *
 * One webhook token for the whole deployment, owned by one account -- no
 * per-user OAuth anywhere. `/message` posts as the bot into the target
 * user's DM. (`/incoming` is a different thing entirely: it feeds params to
 * a Deluge handler and posts nothing by itself.)
 *
 * v2, not v3, for one reason that outranks every other difference: v3 does
 * not deliver to the user you ask for. It takes `user_ids` as a QUERY
 * parameter, and under a zapikey it discards it -- answering 200 and posting
 * to the token's owner whoever you name. Measured three ways against the live
 * endpoint: a colleague's zuid, his email address, and the bogus id
 * 999999999999 all arrived in the owner's DM. v2 takes `userids` in the BODY
 * and honours it, confirmed by two sends landing in two different chat_ids.
 *
 * The trap that produced the v3 version of this file: `user_ids` in the v3
 * BODY is a hard 400 (`extra_key_found`), which reads as proof that it must
 * belong in the query string. It does go there, and it is then ignored. The
 * response field named `user_ids` reports who was actually written to, not
 * who was asked for, so it looks like a confirmation and is not.
 *
 * What v2 costs, measured rather than assumed:
 *   - nothing on formatting. Both versions render CLIQ's dialect, not
 *     CommonMark, and format.js already emits it -- see its header.
 *   - nothing on `buttons`. The claim that they were v3-only was wrong; a
 *     button sent through v2 rendered and was tappable.
 *   - unknown body keys are IGNORED here, not rejected. v3's hard 400 was a
 *     crude form of schema checking, and it is gone, so cliq.test.js is now
 *     the only thing that catches a malformed send.
 *
 * What v2 adds, unused for now: `sync_message` returns a message_id and
 * chat_id per recipient, which v3 refuses. Editing a sent message needs a
 * chat-scoped call a zapikey cannot make (401), so the ids have no consumer
 * yet.
 */

const { buildMultipart } = require('./multipart');

// The JSON message endpoint only. The file endpoint is multipart and ignores
// this entirely. `card` and `bot` were carried here as "the set v3 accepts";
// no caller has ever populated either, so they are gone -- the allow-list
// exists to keep junk off the wire, not to catalogue the API.
const ALLOWED_KEYS = ['slides', 'buttons'];

function createCliq({ baseUrl, botName, zapikey, fetchImpl = globalThis.fetch }) {
  const base = (baseUrl || 'https://cliq.zoho.com').replace(/\/$/, '');

  return {
    /**
     * DM a user as the bot.
     *
     * `zuid` is the recipient's Cliq user id. v2 documents the field as
     * accepting emails too, but the id is what was measured working and it is
     * the one identifier always on a device record -- Cliq grants a bot the
     * subscriber's email only by a consent that lapses.
     *
     * `extras` may carry slides / buttons; anything else is dropped. v2 would
     * ignore an unknown key rather than reject it, so this filter is now a
     * discipline rather than a guard against a 400.
     */
    async dm(zuid, text, extras = {}) {
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

      // In the BODY, and spelled without the underscore. Both of those matter:
      // `user_ids` is v3's name and v2 does not recognise it, and a recipient
      // in the query string is silently dropped. Either mistake delivers to
      // the token's owner with a 200 and no other signal.
      const body = { text, userids: String(zuid) };
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

    /**
     * Send a FILE to one subscriber's DM, as the bot.
     *
     * A different API version from `dm()` on purpose: v3 `/messages` takes
     * JSON and hard-errors on an unknown key, so a file cannot ride there.
     * v2 `/bots/<bot>/files` is multipart and was measured accepting the same
     * webhook zapikey -- no OAuth token is needed.
     *
     * Measured against the live endpoint, none of it documented:
     *   comments  must be a JSON **array**; a bare string is refused
     *   title     refused outright as "an extra param in the query"
     *   buttons   refused even as a plain array, though `comments` accepts
     *             the identical shape -- so the choices that go with a
     *             document have to follow as a separate dm().
     */
    async dmFile(zuid, { filename, bytes, contentType, caption = '' }) {
      if (!botName || !zapikey) {
        const err = new Error('relay is not connected to a Cliq bot yet '
                            + '(CLIQ_BOT_NAME / CLIQ_ZAPIKEY unset)');
        err.status = 503;
        throw err;
      }

      const url = `${base}/api/v2/bots/${encodeURIComponent(botName)}/files`
                + `?zapikey=${encodeURIComponent(zapikey)}`
                + `&user_id=${encodeURIComponent(zuid)}`;

      const { body, contentType: bodyType } = buildMultipart({
        // JSON.stringify of an array, because that is what it wants.
        fields: caption ? { comments: JSON.stringify([caption]) } : {},
        files: [{ name: 'files', filename, contentType, bytes }],
      });

      const res = await fetchImpl(url, {
        method: 'POST',
        headers: { 'Content-Type': bodyType },
        body,
      });
      const raw = await res.text();
      if (!res.ok) {
        const err = new Error(`cliq ${res.status}: ${raw.slice(0, 300)}`);
        err.status = res.status;
        throw err;
      }
      try { return JSON.parse(raw); } catch { return { raw }; }
    },
  };
}

module.exports = { createCliq, ALLOWED_KEYS };
