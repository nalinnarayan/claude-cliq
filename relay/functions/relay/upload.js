'use strict';
/**
 * What arrives when Deluge uploads a file, and how an attachment is named.
 *
 * `invokeurl ... files:` is the only durable way to get an attachment's bytes
 * out of Cliq, and nothing documents how it frames them -- multipart/form-data
 * is the obvious guess, a raw body is the other possibility. Rather than spend
 * a round trip (and a paste) discovering which, this handles both: if the
 * content type says multipart, the bytes are unwrapped from the part that
 * carries a filename; otherwise the body IS the file.
 *
 * Pure, so the shape that actually turns up can be pinned in a test the moment
 * it is seen, instead of being inferred again later.
 */

/** The marker the relay appends so the laptop knows a file came with a reply. */
const MARKER = /\n?\[attached: ([^\]\n]{1,120})\]\s*$/;

/**
 * A filename safe to write to disk and to embed in a one-line marker.
 *
 * Attachment names come from whatever device sent them, so this strips path
 * separators (`../` reaching out of the download directory), control
 * characters, and the `]` that would end the marker early.
 */
function safeName(name, fallback = 'attachment') {
  const base = String(name || '').split(/[\\/]/).pop() || '';
  const clean = base
    .replace(/[\u0000-\u001f\u007f]/g, '')
    .replace(/[\[\]]/g, '')
    .replace(/^\.+/, '')
    .trim()
    .slice(0, 80);
  return clean || fallback;
}

/** Append the marker to a reply's text, having removed any the user typed. */
function withMarker(text, fileName) {
  const body = String(text || '').replace(MARKER, '').trimEnd();
  const name = safeName(fileName);
  return body ? `${body}\n[attached: ${name}]` : `[attached: ${name}]`;
}

function markerName(text) {
  const m = MARKER.exec(String(text || ''));
  return m ? m[1] : '';
}

function stripMarker(text) {
  return String(text || '').replace(MARKER, '').trimEnd();
}

function boundaryOf(contentType) {
  const m = /boundary=(?:"([^"]+)"|([^;\s]+))/i.exec(String(contentType || ''));
  return m ? (m[1] || m[2]) : '';
}

/**
 * Pull the file out of an upload, whatever framing it used.
 *
 * Returns {bytes, name, contentType}. `name` is empty when the framing did not
 * carry one -- the caller has the Cliq filename from the JSON reply anyway, so
 * this is a cross-check rather than the source of truth.
 */
function unwrapUpload(contentType, body) {
  // Never Buffer.from() whatever turns up. When express leaves req.body as
  // the empty object it defaults to -- which is what a request carrying NO
  // BODY produces -- Buffer.from({}) throws "The first argument must be of
  // type string or an instance of Buffer...", an internal Node error that
  // reached a user in Cliq as the explanation for a failed video.
  let buf = Buffer.alloc(0);
  if (Buffer.isBuffer(body)) buf = body;
  else if (typeof body === 'string') buf = Buffer.from(body);
  const ct = String(contentType || '');
  if (!/^multipart\//i.test(ct)) {
    return { bytes: buf, name: '', contentType: ct, framing: 'raw' };
  }

  const boundary = boundaryOf(ct);
  if (!boundary) return { bytes: buf, name: '', contentType: ct, framing: 'multipart-no-boundary' };

  const sep = Buffer.from(`--${boundary}`);
  const parts = [];
  let at = buf.indexOf(sep);
  while (at !== -1) {
    const start = at + sep.length;
    const next = buf.indexOf(sep, start);
    parts.push(buf.subarray(start, next === -1 ? buf.length : next));
    if (next === -1) break;
    at = next;
  }

  for (const part of parts) {
    // Headers end at the first blank line; CRLF per the RFC, but be forgiving.
    let split = part.indexOf('\r\n\r\n');
    let gap = 4;
    if (split === -1) { split = part.indexOf('\n\n'); gap = 2; }
    if (split === -1) continue;

    const headers = part.subarray(0, split).toString('utf8');
    if (!/filename=/i.test(headers)) continue;          // a plain field, not the file

    const name = (/filename=(?:"([^"]*)"|([^;\r\n]+))/i.exec(headers) || [])
      .slice(1).find(Boolean) || '';
    const partType = (/content-type:\s*([^\r\n]+)/i.exec(headers) || [])[1] || '';

    // The framing puts exactly one CRLF between the content and the boundary
    // that follows it, so strip exactly that -- never "all trailing newlines".
    // These are arbitrary bytes: a PNG ends on a CRC, and a file whose last
    // byte happened to be 0x0a would be silently truncated.
    let bytes = part.subarray(split + gap);
    if (bytes.length >= 2 && bytes[bytes.length - 2] === 0x0d && bytes[bytes.length - 1] === 0x0a) {
      bytes = bytes.subarray(0, bytes.length - 2);
    } else if (bytes.length >= 1 && bytes[bytes.length - 1] === 0x0a) {
      bytes = bytes.subarray(0, bytes.length - 1);
    }
    return { bytes, name: name.trim(), contentType: partType.trim(), framing: 'multipart' };
  }

  return { bytes: Buffer.alloc(0), name: '', contentType: ct, framing: 'multipart-no-file' };
}

/** A rough check that what arrived is the image it claims to be. */
function sniff(bytes) {
  const b = Buffer.isBuffer(bytes) ? bytes : Buffer.alloc(0);
  if (b.length >= 8 && b.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) return 'image/png';
  if (b.length >= 3 && b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return 'image/jpeg';
  if (b.length >= 6 && b.subarray(0, 6).toString('latin1').startsWith('GIF8')) return 'image/gif';
  if (b.length >= 12 && b.subarray(0, 4).toString('latin1') === 'RIFF'
      && b.subarray(8, 12).toString('latin1') === 'WEBP') return 'image/webp';
  if (b.length >= 4 && b.subarray(0, 4).toString('latin1') === '%PDF') return 'application/pdf';
  return '';
}

module.exports = { unwrapUpload, safeName, withMarker, markerName, stripMarker, sniff, boundaryOf, MARKER };
