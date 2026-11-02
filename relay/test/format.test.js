'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { splitTables, toCliqMarkdown, splitRow, normaliseHeaders } = require('../functions/relay/format.js');

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

test('prose with no table keeps its shape, dialect aside', () => {
  // Not byte-identical any more: emphasis is converted to Cliq's dialect and
  // the bullet marker with it. What must survive is the structure -- no
  // slides, nothing reflowed, the blank line kept.
  const src = 'Just **bold** and `code`.\n\n- a bullet';
  const { text, slides } = splitTables(src);
  assert.strictEqual(slides.length, 0);
  assert.strictEqual(text, 'Just *bold* and `code`.\n\n\u2022 a bullet');
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

// Cliq's dialect: every doubled marker is one too many. Measured against the
// live bot -- `**bold**` renders bold with the inner asterisks still visible.
test('doubled emphasis markers are halved for Cliq', () => {
  assert.strictEqual(toCliqMarkdown('**bold**'), '*bold*');
  assert.strictEqual(toCliqMarkdown('~~gone~~'), '~gone~');
  assert.strictEqual(toCliqMarkdown('**a** and **b**'), '*a* and *b*');
});

test('__bold__ becomes an asterisk pair, not an underscore one', () => {
  // Cliq renders __x__ as an underline, and _x_ as italics; CommonMark means
  // bold by it, and bold in Cliq is *x*.
  assert.strictEqual(toCliqMarkdown('__bold__'), '*bold*');
});

test('single markers and ordinary prose are left alone', () => {
  for (const src of ['*bold*', '_italic_', '~strike~', 'snake_case_name',
                     'no markers at all']) {
    assert.strictEqual(toCliqMarkdown(src), src);
  }
});

test('emphasis inside code survives untouched', () => {
  assert.strictEqual(toCliqMarkdown('`**kwargs`'), '`**kwargs`');
  assert.strictEqual(toCliqMarkdown('```\nf(**kw)\n```'), '```\nf(**kw)\n```');
});

test('a delimiter run that hugs nothing is not emphasis', () => {
  // Without CommonMark's hug rule these swallow the rest of the message.
  assert.strictEqual(toCliqMarkdown('a ** b ** c'), 'a ** b ** c');
  assert.strictEqual(toCliqMarkdown('2 ** 8 = 256'), '2 ** 8 = 256');
});

test('markdown bullets become bullet characters', () => {
  // Cliq has no typed list syntax, so a hyphen arrives as a hyphen. All three
  // markdown markers mean the same list.
  assert.strictEqual(toCliqMarkdown('- one\n* two\n+ three'),
                     '\u2022 one\n\u2022 two\n\u2022 three');
});

test('bullet indentation is preserved for nested lists', () => {
  assert.strictEqual(toCliqMarkdown('- top\n  - nested\n    - deeper'),
                     '\u2022 top\n  \u2022 nested\n    \u2022 deeper');
});

test('numbered lists are left exactly as they are', () => {
  // Load-bearing: ask.py and plan.py emit `{i}. {label}` and askform.js parses
  // it back to rebuild the form. Renumbering or restyling breaks answering.
  assert.strictEqual(toCliqMarkdown('1. first\n2. second'), '1. first\n2. second');
});

test('what only looks like a bullet is not one', () => {
  // The space after the marker is the whole guard. A horizontal rule, a table
  // divider and a line opening with emphasis all lack it.
  assert.strictEqual(toCliqMarkdown('---'), '---');
  assert.strictEqual(toCliqMarkdown('|---|---|'), '|---|---|');
  assert.strictEqual(toCliqMarkdown('**bold** opens the line'), '*bold* opens the line');
  assert.strictEqual(toCliqMarkdown('- '), '- ', 'a marker with no content');
  assert.strictEqual(toCliqMarkdown('5 - 3 = 2'), '5 - 3 = 2', 'mid-line hyphen');
});

test('a bullet inside code is still code', () => {
  assert.strictEqual(toCliqMarkdown('```\n- not a list\n```'), '```\n- not a list\n```');
  assert.strictEqual(toCliqMarkdown('a `- flag` span'), 'a `- flag` span');
});

test('splitTables converts the dialect in prose and in cells', () => {
  const { text, slides } = splitTables(
    '**Verdict** below\n\n| What | **State** |\n| --- | --- |\n| bold | **broken** |');
  assert.match(text, /\*Verdict\*/);
  assert.doesNotMatch(text, /\*\*/, 'no doubled markers reach Cliq');
  assert.strictEqual(slides[0].data.headers[1], '*State*');
  assert.strictEqual(slides[0].data.rows[0]['*State*'], '*broken*');
});
