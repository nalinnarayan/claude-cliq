'use strict';
/**
 * Turning Claude's questions into a Cliq form, and a submission back again.
 *
 * The relay stores nothing between posting the question and the person tapping
 * Answer. It cannot: a button's `arguments` cap at 300 characters and a
 * four-question spec is roughly 1.2KB. What it can do is read the message back
 * -- a Cliq button function is handed `message.get("text")` exactly as posted,
 * markdown markers and all -- and rebuild the form from text it rendered
 * itself.
 *
 * That makes the message format load-bearing in a way a message format usually
 * is not. It is the spec. `parseQuestions` must invert what the laptop's
 * ask.render() produced, so the round-trip test in askform.test.js is the
 * thing standing between a layout tweak and answering silently breaking.
 *
 * Every limit below was measured against the live bot, not read:
 *   radio    2-5 options      checkbox 1-5 (+ max_selections)
 *   option label  <= 100      option value <= 50
 *   field label   <= 50       hint         <= 100
 *   form name     <= 50       inputs       <= 25
 *
 * And the free-text field, which is documented rather than measured:
 *   type "text"      max_length capped at 100 -- the whole field, not a default
 *   type "textarea"  max_length up to 5000, defaulting to 1000
 *   textarea `mandatory` defaults to TRUE, so it must be set false explicitly
 */

// A question line is bold and alone; an option line is "<n>. <label>"; a
// description is indented with em spaces and italic. Anchored so a question
// whose text happens to contain "1. " cannot be mistaken for an option.
const QUESTION_RE = /^\*(.+)\*$/;
const OPTION_RE = /^(\d+)\.\s+(.+)$/;
const DESCRIPTION_RE = /^[ \s]+_/;

const MAX_OPTION_LABEL = 100;
const MAX_FIELD_LABEL = 50;
const MAX_HINT = 100;
const MAX_INPUTS = 25;
const RADIO_MAX = 5;
// What someone may type instead of picking. A `text` field cannot exceed 100
// characters at all; a `textarea` takes up to 5000.
const MAX_TYPED = 5000;

const OTHER_LABEL = 'Something else — type it below';

/**
 * Split a question across the label and the hint beside it.
 *
 * A field label is capped at 50 characters -- documented AND measured, so
 * there is no lifting it, and a Cliq form has no section header or
 * description element to put the rest in. The per-field hint is the only
 * other text there is.
 *
 * The first version filled that hint with the SAME opening 50 characters the
 * label already showed, so a long question was truncated twice and its tail
 * appeared nowhere. This cuts at a word boundary and gives the hint the
 * REMAINDER, so between them they carry about 150 characters.
 *
 * The full text is in the message the form was opened from either way -- that
 * is where someone reads the question properly. This is about not stranding
 * the end of it.
 */
function splitLabel(question) {
  const text = String(question || '').trim();
  if (text.length <= MAX_FIELD_LABEL) return { label: text, hint: 'Pick one' };

  // Break on the last space that leaves room for the ellipsis, so a label
  // ends on a word rather than mid-syllable.
  const room = MAX_FIELD_LABEL - 1;
  const cut = text.lastIndexOf(' ', room);
  const at = cut > MAX_FIELD_LABEL / 2 ? cut : room;
  return {
    label: `${text.slice(0, at).trimEnd()}…`,
    hint: `…${text.slice(at).trim()}`.slice(0, MAX_HINT),
  };
}

/**
 * Read questions back out of the message text.
 *
 * Returns [{question, options:[label]}]. Descriptions are dropped: they exist
 * for the person reading the message, and a form has nowhere to put them.
 */
function parseQuestions(text) {
  const out = [];
  for (const raw of String(text || '').split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    if (DESCRIPTION_RE.test(raw)) continue;          // option description

    const q = QUESTION_RE.exec(line);
    if (q) {
      out.push({ question: q[1].trim(), options: [] });
      continue;
    }
    const o = OPTION_RE.exec(line);
    if (o && out.length) {
      out[out.length - 1].options.push(o[2].trim());
    }
    // Anything else is the heading or prose around the list, and is ignored.
  }
  return out.filter((x) => x.options.length);
}

/** Does this question want more than one answer? Marked in the rendered text. */
function isMulti(question) {
  return / \(choose any\)$/.test(question);
}

function stripMulti(question) {
  return question.replace(/ \(choose any\)$/, '');
}

/**
 * Build the form Cliq renders.
 *
 * `requestId` becomes the form's `name`, which is the whole binding scheme:
 * a submission comes back carrying it, so it can never be mistaken for an
 * answer to another question. Measured -- the name round-trips exactly.
 */
function buildForm(requestId, questions, { submitFn = 'claudeCliqAskSubmit' } = {}) {
  const inputs = [];
  questions.slice(0, Math.floor(MAX_INPUTS / 2)).forEach((q, i) => {
    const multi = isMulti(q.question);
    const label = stripMulti(q.question);
    const options = q.options.map((text, n) => ({
      label: String(text).slice(0, MAX_OPTION_LABEL),
      // The INDEX, not the text. The laptop maps it back to the original
      // label, so a label truncated at 100 characters never reaches Claude.
      value: String(n),
    }));

    // radio needs at least two options and allows at most five. A
    // single-option question would otherwise fail the WHOLE form at open
    // time, after the tap -- so Other pads it. At the ceiling, Other is
    // dropped and the text box carries it instead.
    if (options.length < RADIO_MAX) {
      options.push({ label: OTHER_LABEL, value: 'other' });
    }

    const split = splitLabel(label);
    inputs.push({
      type: multi ? 'checkbox' : 'radio',
      name: `q${i}`,
      label: split.label,
      hint: split.hint,
      mandatory: !multi,
      options,
      ...(multi ? { max_selections: options.length } : {}),
    });
    // `textarea`, not `text`. A single-line `text` field caps max_length at
    // 100 characters -- documented, and the reason a typed answer felt so
    // cramped. A textarea allows up to 5000 and defaults to 1000.
    //
    // `mandatory: false` is NOT optional here: a textarea's mandatory
    // defaults to TRUE, so leaving it out would demand an answer in the box
    // on every form.
    inputs.push({
      type: 'textarea',
      name: `q${i}_other`,
      label: 'Something else',
      hint: 'Only if you picked the last option',
      placeholder: 'your own answer',
      mandatory: false,
      max_length: MAX_TYPED,
    });
  });

  return {
    type: 'form',
    title: 'Answer Claude',
    name: String(requestId).slice(0, 50),
    hint: 'Your pick goes straight back to the session that asked.',
    button_label: 'Send',
    version: 1,
    // FALSE: dismissing the form must do nothing at all.
    //
    // It was true so that a dismissal released the session at once rather than
    // holding it for the rest of its window. That read a dismissal as a
    // decision, and it usually is not one -- the common reason to close a form
    // is to re-read the question behind it. Reported with a screenshot: the
    // bot said "Cancelled", the question went back to the terminal, and the
    // next answer from Cliq was accepted and acknowledged while nothing was
    // listening.
    //
    // Off, rather than on-and-ignored, so there is no code path to get wrong:
    // Cliq simply does not call us. Releasing the session has its own button
    // on the message now, where the intent is stated rather than inferred.
    trigger_on_cancel: false,
    action: { type: 'invoke.function', name: submitFn, mode: 'immediate' },
    inputs,
  };
}

/**
 * Turn a submission into {questionIndex: [indexes or free text]}.
 *
 * Measured shape: a radio returns an OBJECT `{label, value}`, a checkbox a
 * comma-joined list of values, and an empty text input is `""` rather than
 * absent. On a cancel, `values` is missing entirely.
 */
/**
 * Pull the selected option values out of whatever Cliq hands back.
 *
 * A radio was measured returning an OBJECT, `{label, value}`. A checkbox with
 * several selections was ASSUMED to return a comma-joined string, and that
 * assumption lost real selections: if it returns an array instead, an array is
 * an object, so reading `.value` off it gives undefined and every pick
 * disappears -- leaving only the typed "Other" text, which is exactly what a
 * user reported.
 *
 * So every plausible shape is handled rather than one guessed at: a string, a
 * {label, value} object, an array of either, and a comma-joined string inside
 * any of them.
 */
function valuesOf(value) {
  if (value == null) return [];
  if (Array.isArray(value)) return value.flatMap(valuesOf);
  if (typeof value === 'object') {
    return 'value' in value ? valuesOf(value.value) : [];
  }
  return String(value).split(',').map((x) => x.trim()).filter(Boolean);
}


function readSubmission(values) {
  const picked = {};
  const other = {};
  for (const [field, value] of Object.entries(values || {})) {
    const m = /^q(\d+)(_other)?$/.exec(field);
    if (!m) continue;
    const idx = m[1];
    if (m[2]) {
      if (String(value || '').trim()) other[idx] = String(value).trim();
      continue;
    }
    const parts = valuesOf(value);
    if (parts.length) picked[idx] = parts;
  }
  // "Other" is the chosen option plus whatever was typed; the typed text
  // replaces the marker rather than joining it.
  for (const [idx, text] of Object.entries(other)) {
    const chosen = (picked[idx] || []).filter((p) => p !== 'other');
    picked[idx] = chosen.concat([text]);
  }
  for (const [idx, chosen] of Object.entries(picked)) {
    picked[idx] = chosen.filter((p) => p !== 'other');
    if (!picked[idx].length) delete picked[idx];
  }
  return picked;
}

/**
 * Which questions were answered with "Other" but nothing typed.
 *
 * Returned as a Cliq `form_error`, the one validation that has to happen in
 * the submit handler because that is the only place Cliq accepts one.
 */
function missingOther(values) {
  const inputs = {};
  for (const [field, value] of Object.entries(values || {})) {
    const m = /^q(\d+)$/.exec(field);
    if (!m) continue;
    if (!valuesOf(value).includes('other')) continue;
    const typed = String(values[`q${m[1]}_other`] || '').trim();
    if (!typed) inputs[`q${m[1]}_other`] = 'Type your answer, or pick one of the options.';
  }
  return Object.keys(inputs).length
    ? { type: 'form_error', text: 'Almost there.', inputs }
    : null;
}

module.exports = {
  parseQuestions, buildForm, readSubmission, missingOther, valuesOf, splitLabel,
  isMulti, stripMulti, OTHER_LABEL,
  MAX_OPTION_LABEL, MAX_FIELD_LABEL, MAX_HINT, MAX_INPUTS, RADIO_MAX, MAX_TYPED,
};
