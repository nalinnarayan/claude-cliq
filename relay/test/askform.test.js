'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { execFileSync } = require('child_process');
const path = require('path');
const {
  parseQuestions, buildForm, readSubmission, missingOther,
  isMulti, RADIO_MAX, MAX_FIELD_LABEL, MAX_OPTION_LABEL, MAX_INPUTS,
} = require('../functions/relay/askform.js');

const SCRIPTS = path.join(__dirname, '..', '..', 'scripts');

/** Render through the REAL laptop-side renderer, so the two cannot drift. */
function renderInPython(questions) {
  return execFileSync('python3', ['-c', `
import json, sys
sys.path.insert(0, ${JSON.stringify(SCRIPTS)})
import ask
print(ask.render(json.loads(sys.argv[1])), end="")
`, JSON.stringify(questions)], { encoding: 'utf8' });
}

const Q = (question, options, multiSelect = false) => ({
  question, multiSelect,
  options: options.map((o) => (typeof o === 'string' ? { label: o } : o)),
});

// ---------------------------------------------------------------------------
// The round trip. This is the one that matters: the relay rebuilds the form
// from text the laptop rendered, so a layout change breaks answering silently.
// ---------------------------------------------------------------------------

test('questions survive a round trip through the message text', () => {
  const questions = [
    Q('Which store should the queue use?', ['Switch to Data Store', 'Keep NoSQL']),
    Q('Which triggers should notify you?', ['Task finished', 'Needs input'], true),
  ];
  const parsed = parseQuestions(renderInPython(questions));
  assert.strictEqual(parsed.length, 2);
  assert.strictEqual(parsed[0].question, 'Which store should the queue use?');
  assert.deepStrictEqual(parsed[0].options, ['Switch to Data Store', 'Keep NoSQL']);
  assert.ok(isMulti(parsed[1].question), 'the multiSelect marker survives');
});

test('option descriptions are dropped, not mistaken for options', () => {
  // They exist for the person reading the message; a form has nowhere to put
  // them. Indented and italic so the parser can tell them apart.
  const parsed = parseQuestions(renderInPython([
    Q('Pick one', [{ label: 'First', description: 'A long explanation of it.' },
                   { label: 'Second', description: 'Another one.' }]),
  ]));
  assert.deepStrictEqual(parsed[0].options, ['First', 'Second']);
});

test('a question containing digits and a full stop is not read as an option', () => {
  // "1. " inside the question text is the obvious way this parser breaks.
  const parsed = parseQuestions(renderInPython([
    Q('Step 1. Which of these?', ['Yes', 'No']),
  ]));
  assert.strictEqual(parsed.length, 1, 'one question, not two');
  assert.strictEqual(parsed[0].question, 'Step 1. Which of these?');
  assert.deepStrictEqual(parsed[0].options, ['Yes', 'No']);
});

test('a question containing asterisks or newlines still round-trips', () => {
  const parsed = parseQuestions(renderInPython([
    Q('Use *bold* here\nand a second line?', ['A', 'B']),
  ]));
  assert.strictEqual(parsed.length, 1);
  assert.match(parsed[0].question, /bold/);
  assert.deepStrictEqual(parsed[0].options, ['A', 'B']);
});

test('the heading and surrounding prose are ignored', () => {
  const text = '❓ Claude is asking — …/claude-cliq · main\n\n'
             + renderInPython([Q('Which one?', ['A', 'B'])]);
  assert.deepStrictEqual(parseQuestions(text).map((q) => q.question), ['Which one?']);
});

test('text with no question list parses to nothing rather than guessing', () => {
  assert.deepStrictEqual(parseQuestions('just a sentence'), []);
  assert.deepStrictEqual(parseQuestions(''), []);
});

// ---------------------------------------------------------------------------
// Building the form, against the measured Cliq limits.
// ---------------------------------------------------------------------------

test('the request id becomes the form name, which is the whole binding', () => {
  const form = buildForm('abcde', [{ question: 'Q', options: ['A', 'B'] }]);
  assert.strictEqual(form.name, 'abcde');
  assert.strictEqual(form.action.type, 'invoke.function');
  assert.strictEqual(form.trigger_on_cancel, true, 'so a dismissal releases the session');
});

test('options carry their INDEX, never the label', () => {
  // A label is truncated at Cliq's 100 characters; an index cannot be. The
  // laptop maps the index back to the original label.
  const form = buildForm('abcde', [{ question: 'Q', options: ['First', 'Second'] }]);
  assert.deepStrictEqual(form.inputs[0].options.slice(0, 2),
    [{ label: 'First', value: '0' }, { label: 'Second', value: '1' }]);
});

test('a one-option question is padded, not left to fail at open time', () => {
  // radio requires two. One option would 400 the WHOLE form after the tap.
  const form = buildForm('abcde', [{ question: 'Q', options: ['Only'] }]);
  assert.strictEqual(form.inputs[0].options.length, 2);
  assert.strictEqual(form.inputs[0].options[1].value, 'other');
});

test('at the ceiling Other is dropped rather than overflowing radio', () => {
  const five = ['a', 'b', 'c', 'd', 'e'];
  const form = buildForm('abcde', [{ question: 'Q', options: five }]);
  assert.strictEqual(form.inputs[0].options.length, RADIO_MAX);
  assert.ok(!form.inputs[0].options.some((o) => o.value === 'other'),
            'the text box carries it instead');
});

test('a multiSelect question becomes a checkbox', () => {
  const form = buildForm('abcde',
    [{ question: 'Q (choose any)', options: ['A', 'B'] }]);
  assert.strictEqual(form.inputs[0].type, 'checkbox');
  assert.strictEqual(form.inputs[0].label, 'Q', 'the marker is not shown to the reader');
  assert.strictEqual(form.inputs[0].mandatory, false, 'choosing none is allowed');
});

test('every field respects the measured caps', () => {
  const long = 'x'.repeat(300);
  const form = buildForm('abcde', [{ question: long, options: [long, 'B'] }]);
  for (const input of form.inputs) {
    assert.ok(input.label.length <= MAX_FIELD_LABEL, `label ${input.label.length}`);
    assert.ok((input.hint || '').length <= 100, 'hint');
    for (const o of input.options || []) {
      assert.ok(o.label.length <= MAX_OPTION_LABEL, 'option label');
      assert.ok(o.value.length <= 50, 'option value');
    }
  }
  assert.ok(form.name.length <= 50);
});

test('too many questions are truncated to stay inside the input cap', () => {
  const many = Array.from({ length: 20 }, (_, i) => ({ question: `Q${i}`, options: ['A', 'B'] }));
  const form = buildForm('abcde', many);
  assert.ok(form.inputs.length <= MAX_INPUTS, `${form.inputs.length} inputs`);
});

// ---------------------------------------------------------------------------
// Reading a submission, against the measured shapes.
// ---------------------------------------------------------------------------

test('a radio returns an object, and its value is taken', () => {
  // Measured: {"q0": {"label": "Option one", "value": "0"}}
  assert.deepStrictEqual(
    readSubmission({ q0: { label: 'Option one', value: '0' }, q0_other: '' }),
    { 0: ['0'] });
});

test('a checkbox returns a comma-joined list', () => {
  assert.deepStrictEqual(readSubmission({ q0: '0,2' }), { 0: ['0', '2'] });
});

test('Other replaces the marker with what was typed', () => {
  assert.deepStrictEqual(
    readSubmission({ q0: { label: '…', value: 'other' }, q0_other: 'my own answer' }),
    { 0: ['my own answer'] });
});

test('Other alongside real picks keeps both', () => {
  assert.deepStrictEqual(
    readSubmission({ q0: '1,other', q0_other: 'and this' }),
    { 0: ['1', 'and this'] });
});

test('Other chosen with nothing typed yields no answer, and an error', () => {
  const values = { q0: { label: '…', value: 'other' }, q0_other: '  ' };
  assert.deepStrictEqual(readSubmission(values), {},
    'better unanswered than answered with the marker');
  const err = missingOther(values);
  assert.strictEqual(err.type, 'form_error');
  assert.ok(err.inputs.q0_other, 'and it points at the box to fill');
});

test('a complete submission needs no error', () => {
  assert.strictEqual(missingOther({ q0: { value: '1' }, q0_other: '' }), null);
});

test('a cancel has no values at all and must not throw', () => {
  // Measured: {"name": "...", "action": "cancel"} -- `values` absent entirely.
  assert.deepStrictEqual(readSubmission(undefined), {});
  assert.strictEqual(missingOther(undefined), null);
});

test('an unanswered question is omitted rather than sent empty', () => {
  assert.deepStrictEqual(readSubmission({ q0: '', q0_other: '', q1: '2' }), { 1: ['2'] });
});
