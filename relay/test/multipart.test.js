'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { buildMultipart, newBoundary } = require('../functions/relay/multipart.js');
const { unwrapUpload } = require('../functions/relay/upload.js');

const PNG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3]);

// ---------------------------------------------------------------------------
// The round trip. The parser already exists, so building and parsing can be
// held against each other rather than both being guessed at.
// ---------------------------------------------------------------------------

test('what is built parses back to what went in', () => {
  const { body, contentType } = buildMultipart({
    fields: { comments: '["look at this"]' },
    files: [{ name: 'files', filename: 'plan.md', contentType: 'text/markdown',
              bytes: Buffer.from('# A plan\n\nwith a body.\n') }],
  });
  const got = unwrapUpload(contentType, body);
  assert.strictEqual(got.framing, 'multipart');
  assert.strictEqual(got.name, 'plan.md');
  assert.strictEqual(got.contentType, 'text/markdown');
  assert.strictEqual(got.bytes.toString(), '# A plan\n\nwith a body.\n');
});

test('binary survives byte for byte, including bytes the framing uses', () => {
  // A file ending on CRLF or a dash is ordinary, not a corner case: the
  // parser's first version trimmed those and would have truncated it.
  const payload = Buffer.concat([PNG, Buffer.from([0x2d, 0x0d, 0x0a])]);
  const { body, contentType } = buildMultipart({
    files: [{ filename: 'shot.png', contentType: 'image/png', bytes: payload }],
  });
  const got = unwrapUpload(contentType, body);
  assert.ok(got.bytes.equals(payload), 'byte for byte');
});

test('a file containing the boundary gets a different boundary', () => {
  // Otherwise the payload ends its own part early and the file is truncated.
  // Random boundaries make this astronomically unlikely rather than
  // impossible, so the builder re-rolls on a collision instead of trusting
  // the odds -- even when the caller pinned one, since a corrupted upload is
  // worse than an unexpected boundary.
  const pinned = newBoundary();
  const bytes = Buffer.from(`a line\n--${pinned}\nanother line\n`);
  const out = buildMultipart({
    boundary: pinned,
    files: [{ filename: 'x.txt', contentType: 'text/plain', bytes }],
  });
  assert.notStrictEqual(out.boundary, pinned, 'it re-rolled');
  assert.ok(unwrapUpload(out.contentType, out.body).bytes.equals(bytes),
            'and the file survives whole');
});

test('each boundary is fresh', () => {
  assert.notStrictEqual(newBoundary(), newBoundary());
  assert.match(newBoundary(), /^----claudeCliq[0-9a-f]{32}$/);
});

// ---------------------------------------------------------------------------
// Fields, and the headers the endpoint reads.
// ---------------------------------------------------------------------------

test('plain fields come before the file and are left verbatim', () => {
  // `comments` must be a JSON ARRAY -- measured: a bare string is refused
  // with "Value passed for the 'comments' key should be an JSONArray".
  const { body } = buildMultipart({
    fields: { comments: '["one","two"]' },
    files: [{ filename: 'a.txt', bytes: Buffer.from('x') }],
  });
  const text = body.toString();
  assert.ok(text.indexOf('name="comments"') < text.indexOf('filename="a.txt"'));
  assert.match(text, /\["one","two"\]/, 'the JSON is not escaped or re-encoded');
});

test('empty and absent fields are omitted rather than sent blank', () => {
  const { body } = buildMultipart({
    fields: { comments: null, other: undefined, kept: '' },
    files: [{ filename: 'a.txt', bytes: Buffer.from('x') }],
  });
  const text = body.toString();
  assert.doesNotMatch(text, /name="comments"/);
  assert.doesNotMatch(text, /name="other"/);
  assert.match(text, /name="kept"/, 'an empty string is still a value');
});

test('a filename cannot break out of its header', () => {
  // The name reaches us from a plan title or a caller; a quote or newline in
  // it would end the Content-Disposition early and corrupt the whole body.
  const { body, contentType } = buildMultipart({
    files: [{ filename: 'ev"il\r\nX-Injected: 1\\.md', bytes: Buffer.from('x') }],
  });
  // The text survives as part of the NAME, which is harmless. What must not
  // survive is the CRLF that would turn it into a header of its own.
  assert.doesNotMatch(body.toString(), /\r\nX-Injected/,
                      'no forged header line');
  assert.strictEqual(unwrapUpload(contentType, body).name, 'evilX-Injected: 1.md');
  assert.strictEqual(unwrapUpload(contentType, body).bytes.toString(), 'x',
                     'and the body is still readable');
});

test('a missing filename and content type still produce a valid part', () => {
  const { body, contentType } = buildMultipart({ files: [{ bytes: Buffer.from('x') }] });
  const got = unwrapUpload(contentType, body);
  assert.strictEqual(got.name, 'attachment');
  assert.strictEqual(got.contentType, 'application/octet-stream');
  assert.strictEqual(got.bytes.toString(), 'x');
});

test('the declared content type carries the boundary it actually used', () => {
  const { contentType, boundary, body } = buildMultipart({
    files: [{ filename: 'a.txt', bytes: Buffer.from('x') }],
  });
  assert.strictEqual(contentType, `multipart/form-data; boundary=${boundary}`);
  assert.ok(body.toString().startsWith(`--${boundary}\r\n`));
  assert.ok(body.toString().endsWith(`--${boundary}--\r\n`), 'and closes properly');
});
