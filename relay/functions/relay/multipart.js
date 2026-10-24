'use strict';
/**
 * Building a multipart/form-data body.
 *
 * The inverse of `unwrapUpload` in upload.js, which takes one apart. Cliq's
 * file endpoint is multipart only, and a Catalyst function has no form-data
 * dependency available -- so the body is assembled here, by hand, where the
 * round trip against the existing parser can be tested.
 *
 * Measured against the live endpoint
 * (`POST /api/v2/bots/<bot>/files?zapikey=...&user_id=...`):
 *
 *   files      the file part; the endpoint takes up to 10 and 50 MB
 *   comments   a JSON **array**, not a string -- a bare string is refused
 *              with "Value passed for the 'comments' key should be an
 *              JSONArray", which is the kind of thing no documentation says
 *   title      REJECTED outright: "'title' is an extra param in the query"
 *   buttons    REJECTED, even as a plain array, although `comments` accepts
 *              the identical shape. So a file message cannot carry buttons
 *              and the choices have to follow as a separate v3 message.
 */

const crypto = require('crypto');

const CRLF = '\r\n';

/** A boundary that cannot appear in the payload. */
function newBoundary() {
  return `----claudeCliq${crypto.randomBytes(16).toString('hex')}`;
}

/**
 * Assemble a multipart body.
 *
 * `fields` are plain form fields; `files` are {name, filename, contentType,
 * bytes}. Returns {body, contentType} ready to hand to fetch.
 */
function buildMultipart({ fields = {}, files = [], boundary = newBoundary() } = {}) {
  // A boundary that occurs inside the payload would end the part early and
  // truncate the file. Thirty-two random hex characters make that
  // astronomically unlikely -- but "unlikely" is not the same as "cannot",
  // and checking costs one scan, so the guarantee is made real instead.
  // Re-rolled even when the caller pinned one: a corrupted upload is worse
  // than an unexpected boundary.
  const collides = (b) => files.some((f) => {
    const bytes = Buffer.isBuffer(f.bytes) ? f.bytes : Buffer.from(f.bytes || '');
    return bytes.includes(`--${b}`);
  });
  while (collides(boundary)) boundary = newBoundary();

  const parts = [];

  for (const [name, value] of Object.entries(fields)) {
    if (value === undefined || value === null) continue;
    parts.push(Buffer.from(
      `--${boundary}${CRLF}`
      + `Content-Disposition: form-data; name="${name}"${CRLF}${CRLF}`
      + `${value}${CRLF}`));
  }

  for (const file of files) {
    const bytes = Buffer.isBuffer(file.bytes) ? file.bytes : Buffer.from(file.bytes || '');
    // The filename is what Cliq shows in the chat and what the extension is
    // read from, so it is quoted and stripped of anything that would end the
    // header early.
    const filename = String(file.filename || 'attachment')
      .replace(/[\r\n"\\]/g, '')
      .slice(0, 120);
    parts.push(Buffer.from(
      `--${boundary}${CRLF}`
      + `Content-Disposition: form-data; name="${file.name || 'files'}"; `
      + `filename="${filename}"${CRLF}`
      + `Content-Type: ${file.contentType || 'application/octet-stream'}${CRLF}${CRLF}`));
    parts.push(bytes);
    parts.push(Buffer.from(CRLF));
  }

  parts.push(Buffer.from(`--${boundary}--${CRLF}`));
  return {
    body: Buffer.concat(parts),
    contentType: `multipart/form-data; boundary=${boundary}`,
    boundary,
  };
}

module.exports = { buildMultipart, newBoundary };
