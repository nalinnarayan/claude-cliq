'use strict';
const test = require('node:test');
const assert = require('node:assert');
const {
  unwrapUpload, safeName, withMarker, markerName, stripMarker, sniff, boundaryOf,
} = require('../functions/relay/upload.js');

const PNG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

function multipart(boundary, parts) {
  const chunks = [];
  for (const p of parts) {
    chunks.push(Buffer.from(`--${boundary}\r\n${p.headers}\r\n\r\n`));
    chunks.push(p.body);
    chunks.push(Buffer.from('\r\n'));
  }
  chunks.push(Buffer.from(`--${boundary}--\r\n`));
  return Buffer.concat(chunks);
}

// ---------------------------------------------------------------------------
// What Deluge sends is the unknown this module exists to absorb.
// ---------------------------------------------------------------------------

test('a raw body is taken as the file itself', () => {
  const got = unwrapUpload('image/png', PNG);
  assert.strictEqual(got.framing, 'raw');
  assert.ok(got.bytes.equals(PNG));
});

test('a multipart body is unwrapped to the part carrying a filename', () => {
  const body = multipart('abc', [
    { headers: 'Content-Disposition: form-data; name="file"; filename="shot.png"\r\n'
             + 'Content-Type: image/png', body: PNG },
  ]);
  const got = unwrapUpload('multipart/form-data; boundary=abc', body);
  assert.strictEqual(got.framing, 'multipart');
  assert.strictEqual(got.name, 'shot.png');
  assert.strictEqual(got.contentType, 'image/png');
  assert.ok(got.bytes.equals(PNG));
});

test('bytes survive EXACTLY, including ones the framing uses', () => {
  // The first version stripped every trailing newline and dash, which would
  // have truncated any file whose last byte happened to be one -- a PNG ends
  // on a CRC, so this is ordinary, not a corner case.
  const payload = Buffer.concat([PNG, Buffer.from([0x2d, 0x0d, 0x0a, 0x2d, 0x2d])]);
  const body = multipart('z', [
    { headers: 'Content-Disposition: form-data; name="f"; filename="a.png"', body: payload },
  ]);
  const got = unwrapUpload('multipart/form-data; boundary=z', body);
  assert.strictEqual(got.bytes.length, payload.length, 'nothing trimmed');
  assert.ok(got.bytes.equals(payload), 'byte-for-byte');
});

test('a plain form field is skipped in favour of the file', () => {
  const body = multipart('q', [
    { headers: 'Content-Disposition: form-data; name="caption"', body: Buffer.from('hello') },
    { headers: 'Content-Disposition: form-data; name="f"; filename="b.jpg"', body: PNG },
  ]);
  const got = unwrapUpload('multipart/form-data; boundary=q', body);
  assert.strictEqual(got.name, 'b.jpg');
  assert.ok(got.bytes.equals(PNG));
});

test('a quoted boundary is read, and a missing one does not throw', () => {
  assert.strictEqual(boundaryOf('multipart/form-data; boundary="a b"'), 'a b');
  assert.strictEqual(boundaryOf('multipart/form-data'), '');
  const got = unwrapUpload('multipart/form-data', PNG);
  assert.strictEqual(got.framing, 'multipart-no-boundary');
  assert.ok(got.bytes.equals(PNG), 'fall back to the whole body rather than nothing');
});

test('multipart carrying no file at all yields nothing, not garbage', () => {
  const body = multipart('n', [
    { headers: 'Content-Disposition: form-data; name="caption"', body: Buffer.from('hi') },
  ]);
  const got = unwrapUpload('multipart/form-data; boundary=n', body);
  assert.strictEqual(got.framing, 'multipart-no-file');
  assert.strictEqual(got.bytes.length, 0, 'so the route answers 400 rather than storing junk');
});

test('bare LF line endings are tolerated', () => {
  const body = Buffer.concat([
    Buffer.from('--x\nContent-Disposition: form-data; name="f"; filename="c.png"\n\n'),
    PNG, Buffer.from('\n--x--\n'),
  ]);
  const got = unwrapUpload('multipart/form-data; boundary=x', body);
  assert.ok(got.bytes.equals(PNG));
});

// ---------------------------------------------------------------------------
// Names arrive from whatever device sent them.
// ---------------------------------------------------------------------------

test('a filename cannot escape the download directory', () => {
  assert.strictEqual(safeName('../../etc/passwd'), 'passwd');
  assert.strictEqual(safeName('..\\..\\windows\\system32\\x.dll'), 'x.dll');
  assert.strictEqual(safeName('/absolute/path.png'), 'path.png');
  assert.strictEqual(safeName('...'), 'attachment', 'leading dots stripped to nothing');
});

test('a filename cannot end the marker early or carry control characters', () => {
  assert.strictEqual(safeName('a]b[c.png'), 'abc.png');
  assert.strictEqual(safeName('new\nline.png'), 'newline.png');
  assert.strictEqual(safeName(''), 'attachment');
  assert.strictEqual(safeName(null), 'attachment');
});

test('a caption cannot forge the attachment marker', () => {
  // The relay appends the marker, so a caption that already contains one must
  // not survive -- otherwise a typed message could point a session at a file.
  const text = withMarker('look at this [attached: evil.png]\n[attached: other.png]', 'real.png');
  assert.strictEqual(markerName(text), 'real.png', 'the real name wins');
  assert.strictEqual((text.match(/\[attached:/g) || []).length, 2,
    'the inline one is not a marker -- only a trailing one is');
  assert.ok(text.endsWith('[attached: real.png]'));
});

test('an empty caption still gets a marker, since the file IS the message', () => {
  assert.strictEqual(withMarker('', 'shot.png'), '[attached: shot.png]');
  assert.strictEqual(withMarker(null, 'shot.png'), '[attached: shot.png]');
});

test('the marker round-trips off the end of the text', () => {
  assert.strictEqual(markerName('hi\n[attached: a.png]'), 'a.png');
  assert.strictEqual(stripMarker('hi\n[attached: a.png]'), 'hi');
  assert.strictEqual(markerName('no file here'), '');
  assert.strictEqual(stripMarker('no file here'), 'no file here');
});

// ---------------------------------------------------------------------------
// A rough check that what arrived is what it claimed.
// ---------------------------------------------------------------------------

test('common image types are recognised and nonsense is not', () => {
  assert.strictEqual(sniff(PNG), 'image/png');
  assert.strictEqual(sniff(Buffer.from([0xff, 0xd8, 0xff, 0xe0])), 'image/jpeg');
  assert.strictEqual(sniff(Buffer.from('GIF89a')), 'image/gif');
  assert.strictEqual(sniff(Buffer.concat([Buffer.from('RIFF'), Buffer.alloc(4), Buffer.from('WEBP')])), 'image/webp');
  assert.strictEqual(sniff(Buffer.from('%PDF-1.4')), 'application/pdf');
  assert.strictEqual(sniff(Buffer.from('not a picture')), '');
  assert.strictEqual(sniff(Buffer.alloc(0)), '');
});
