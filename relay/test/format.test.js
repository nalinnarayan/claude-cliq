'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { splitTables, splitRow, normaliseHeaders } = require('../functions/relay/format.js');

test('a table becomes a slide and leaves the prose behind', () => {
  const { text, slides } = splitTables([
    'Here are the limits:',
    '',
    '| Limit | Value |',
    '| --- | --- |',
    '| Time | 30 min |',
    '| Turns | 6 |',
    '',
    'That is all.',
  ].join('\n'));
  assert.strictEqual(slides.length, 1);
  assert.deepStrictEqual(slides[0].data.headers, ['Limit', 'Value']);
  assert.deepStrictEqual(slides[0].data.rows, [
    { Limit: 'Time', Value: '30 min' },
    { Limit: 'Turns', Value: '6' },
  ]);
  assert.match(text, /Here are the limits/);
  assert.match(text, /That is all/);
  assert.doesNotMatch(text, /\|/, 'no pipes should survive in the prose');
});

test('row keys match the header strings exactly', () => {
  const { slides } = splitTables('| A | B |\n| --- | --- |\n| 1 | 2 |');
  const { headers, rows } = slides[0].data;
  for (const r of rows) assert.deepStrictEqual(Object.keys(r), headers);
});

test('the line above becomes the title when it reads like one', () => {
  const h = splitTables('## Wait limits\n\n| A |\n| --- |\n| 1 |');
  assert.strictEqual(h.slides[0].title, 'Wait limits');
  const c = splitTables('Current limits:\n| A |\n| --- |\n| 1 |');
  assert.strictEqual(c.slides[0].title, 'Current limits');
  const n = splitTables('Just prose here\n\n| A |\n| --- |\n| 1 |');
  assert.strictEqual(n.slides[0].title, undefined);
});

test('pipes inside a code fence are left alone', () => {
  const src = ['```', 'ps aux | grep claude', '```'].join('\n');
  const { text, slides } = splitTables(src);
  assert.strictEqual(slides.length, 0);
  assert.match(text, /ps aux \| grep claude/);
});

test('duplicate and empty headers are made usable', () => {
  assert.deepStrictEqual(normaliseHeaders(['A', 'A', '']), ['A', 'A (2)', 'Column 3']);
  const { slides } = splitTables('| A | A |\n| --- | --- |\n| 1 | 2 |');
  assert.deepStrictEqual(slides[0].data.rows, [{ A: '1', 'A (2)': '2' }]);
});

test('escaped pipes stay inside their cell', () => {
  assert.deepStrictEqual(splitRow('| a \\| b | c |'), ['a | b', 'c']);
});

test('ragged rows are padded rather than dropped', () => {
  const { slides } = splitTables('| A | B | C |\n| --- | --- | --- |\n| 1 | 2 |');
  assert.deepStrictEqual(slides[0].data.rows, [{ A: '1', B: '2', C: '' }]);
});

test('a header with no data rows is left as prose', () => {
  const { text, slides } = splitTables('| A | B |\n| --- | --- |\n\nnothing follows');
  assert.strictEqual(slides.length, 0);
  assert.match(text, /\| A \| B \|/);
});

test('several tables each become their own slide, in order', () => {
  const { slides } = splitTables([
    '| A |', '| --- |', '| 1 |', '',
    '| B |', '| --- |', '| 2 |',
  ].join('\n'));
  assert.strictEqual(slides.length, 2);
  assert.deepStrictEqual(slides.map((s) => s.data.headers[0]), ['A', 'B']);
});

test('prose with no table is returned untouched', () => {
  const src = 'Just **bold** and `code`.\n\n- a bullet';
  const { text, slides } = splitTables(src);
  assert.strictEqual(slides.length, 0);
  assert.strictEqual(text, src);
});

test('the title line is not left stranded in the prose', () => {
  const { text } = splitTables('Wait limits:\n\n| A |\n| --- |\n| 1 |\n\nmore prose');
  assert.doesNotMatch(text, /Wait limits:/, 'the label became the slide title');
});

test('a signpost appears only when prose follows the table', () => {
  const mid = splitTables('intro\n\n| A |\n| --- |\n| 1 |\n\nafter');
  assert.match(mid.text, /shown below/);
  const end = splitTables('intro\n\n| A |\n| --- |\n| 1 |');
  assert.doesNotMatch(end.text, /shown below/, 'nothing follows, so no signpost');
});

test('a message that is only a label and a table still has text', () => {
  const { text, slides } = splitTables('Here are the limits:\n\n| A | B |\n| --- | --- |\n| 1 | 2 |');
  assert.ok(text.length > 0, 'Cliq requires text');
  assert.strictEqual(text, slides[0].title);
});
