'use strict';
/**
 * Turn Markdown into what Cliq can render.
 *
 * Two separate problems. Tables arrive as raw pipes, so they are lifted out
 * and emitted as native table slides. And Cliq's markdown is not CommonMark:
 * every doubled marker is a doubled marker too many. Measured against the
 * live bot (see docs/FINDINGS.md):
 *
 *   **bold**     outer pair consumed, inner asterisks left visible  BROKEN
 *   *bold*       bold
 *   _italic_     italic
 *   __italic__   underline, not bold
 *   ~~strike~~   strikethrough with the tildes left visible         BROKEN
 *   ~strike~     strikethrough
 *   `code`       code
 *   ## Heading   heading
 *   - item       stays a literal hyphen, which reads fine anyway
 *   [t](url)     link
 *
 * Claude writes CommonMark, so the doubled markers have to be halved on the
 * way out. Headings, links and fences need nothing.
 *
 * Pure: no Cliq or network imports, so the parsing is directly testable.
 */

// A Cliq message caps at 5000 characters; stay under it with room for the
// hint and the "(id xxxxx)" footer that a swipe-reply binds to.
const MAX_PART = 4500;
// A long turn becomes several messages rather than one truncated one -- but
// not without bound: Cliq's 50/min is shared across the whole deployment, and
// a 429 locks everyone out for ten minutes. Five parts is ~22,500 characters,
// far more than any real message, and what exceeds it is reported rather than
// silently dropped.
const MAX_PARTS = 5;

const FENCE = /^```/;

/**
 * Break a long message into Cliq-sized parts, preferring paragraph breaks.
 *
 * Replaces truncation. The old behaviour cut the text and appended an
 * ellipsis, so the end of a message -- which is where the point usually is --
 * was lost with no way to ask for it.
 *
 * Splits at a blank line where it can, a line break if it must, and a space as
 * a last resort. A code fence left open by a split is closed and reopened, or
 * the rest of the message renders as one code block.
 *
 * Returns {parts, dropped}: `dropped` is how many characters did not fit in
 * MAX_PARTS, so the caller can say so instead of pretending.
 */
function splitForCliq(text, { limit = MAX_PART, maxParts = MAX_PARTS } = {}) {
  const body = String(text || '');
  if (body.length <= limit) return { parts: body ? [body] : [], dropped: 0 };

  // Blocks, keeping the blank line that separated them.
  const blocks = body.split(/\n{2,}/);
  const parts = [];
  let current = '';

  const flush = () => {
    if (current.trim()) parts.push(current.trimEnd());
    current = '';
  };
  const push = (chunk) => {
    if (!current) { current = chunk; return; }
    if (current.length + 2 + chunk.length <= limit) {
      current += `\n\n${chunk}`;
    } else {
      flush();
      current = chunk;
    }
  };

  for (const block of blocks) {
    if (block.length <= limit) { push(block); continue; }

    // Too big on its own: break it by lines, then by spaces.
    let line = '';
    for (const raw of block.split('\n')) {
      if (line.length + 1 + raw.length <= limit) {
        line = line ? `${line}\n${raw}` : raw;
        continue;
      }
      if (line) { push(line); line = ''; }
      let rest = raw;
      while (rest.length > limit) {
        const cut = rest.lastIndexOf(' ', limit);
        const at = cut > limit * 0.6 ? cut : limit;
        push(rest.slice(0, at).trimEnd());
        rest = rest.slice(at).trimStart();
      }
      line = rest;
    }
    if (line) push(line);
  }
  flush();

  // Repair fences broken by a split, so the remainder is not swallowed into
  // one code block.
  let open = false;
  const mended = parts.map((part) => {
    let out = part;
    if (open) out = `\`\`\`\n${out}`;
    const fences = out.split('\n').filter((l) => FENCE.test(l.trim())).length;
    open = fences % 2 === 1;
    if (open) out = `${out}\n\`\`\``;
    return out;
  });

  const kept = mended.slice(0, maxParts);
  const dropped = mended.slice(maxParts).join('\n\n').length;
  return { parts: kept, dropped };
}

const MAX_SLIDES = 5;          // keep a phone message readable
const MAX_ROWS = 20;           // a long table is unreadable on a phone anyway

// Fences and inline code spans, captured so split() keeps them. Emphasis
// inside them is code -- `**kwargs` in a snippet must survive untouched.
const CODE = /(```[\s\S]*?```|`[^`\n]*`)/g;

/** Halve CommonMark's doubled emphasis markers into Cliq's single ones.
 *
 * The lookahead and the trailing \S are CommonMark's own rule: a delimiter
 * run has to hug its content. Without them `** **` and a lone `**` in prose
 * both swallow the rest of the message.
 */
function toCliqMarkdown(markdown) {
  return String(markdown || '')
    .split(CODE)
    // split() on a capturing regex puts the code parts at odd indices.
    .map((part, i) => (i % 2 ? part : part
      .replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, '*$1*')
      // CommonMark means bold by __this__; Cliq renders it as an underline,
      // so it becomes an asterisk pair rather than an underscore one.
      .replace(/__(?=\S)([\s\S]*?\S)__/g, '*$1*')
      .replace(/~~(?=\S)([\s\S]*?\S)~~/g, '~$1~')))
    .join('');
}

const isDivider = (line) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);
const looksLikeRow = (line) => line.includes('|') && /\|/.test(line.trim());

/** Split a table row, honouring \| escapes and optional outer pipes. */
function splitRow(line) {
  const cells = [];
  let cur = '';
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (ch === '\\' && line[i + 1] === '|') { cur += '|'; i++; continue; }
    if (ch === '|') { cells.push(cur); cur = ''; continue; }
    cur += ch;
  }
  cells.push(cur);
  if (cells.length && cells[0].trim() === '') cells.shift();
  if (cells.length && cells[cells.length - 1].trim() === '') cells.pop();
  return cells.map((c) => c.trim());
}

/**
 * Cliq matches row keys to header strings exactly, so headers must be
 * non-empty and unique or cells silently collide.
 */
function normaliseHeaders(raw) {
  const seen = new Map();
  return raw.map((h, i) => {
    let name = h.trim() || `Column ${i + 1}`;
    if (seen.has(name)) {
      const n = seen.get(name) + 1;
      seen.set(name, n);
      name = `${name} (${n})`;
    } else {
      seen.set(name, 1);
    }
    return name;
  });
}

/**
 * A title for the table, taken from the line above when it reads like one.
 * Returns the source line index too, so the caller can drop that line from
 * the prose -- a bare "Wait limits:" with nothing under it reads as broken,
 * since Cliq renders every slide after the text.
 */
function titleFor(lines, start) {
  for (let i = start - 1; i >= 0 && i >= start - 2; i--) {
    const prev = (lines[i] || '').trim();
    if (!prev) continue;
    const heading = prev.match(/^#{1,6}\s+(.*)$/);
    if (heading) return { title: heading[1].trim().slice(0, 100), from: i };
    if (prev.endsWith(':')) {
      return { title: prev.slice(0, -1).replace(/\*\*/g, '').trim().slice(0, 100), from: i };
    }
    return { title: '', from: -1 };
  }
  return { title: '', from: -1 };
}

/**
 * @returns {{text: string, slides: Array}} prose with tables removed, plus
 *          one slide per table in document order.
 */
function splitTables(markdown) {
  // Dialect first, so table cells are converted along with the prose.
  const lines = toCliqMarkdown(markdown).split('\n');
  const out = [];
  const slides = [];
  const marks = [];          // where each table was lifted from
  let inFence = false;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // Never touch anything inside a code fence — pipes there are code.
    if (/^\s*```/.test(line)) { inFence = !inFence; out.push(line); continue; }
    if (inFence) { out.push(line); continue; }

    const next = lines[i + 1];
    if (looksLikeRow(line) && next !== undefined && isDivider(next)
        && slides.length < MAX_SLIDES) {
      const headers = normaliseHeaders(splitRow(line));
      const rows = [];
      let j = i + 2;
      for (; j < lines.length && looksLikeRow(lines[j]) && lines[j].trim(); j++) {
        if (rows.length >= MAX_ROWS) break;
        const cells = splitRow(lines[j]);
        const row = {};
        headers.forEach((h, k) => { row[h] = cells[k] !== undefined ? cells[k] : ''; });
        rows.push(row);
      }
      if (rows.length) {
        const { title, from } = titleFor(lines, i);
        // The title line is now the slide's heading; leaving it in the prose
        // would duplicate it, or strand a label above nothing. Skip back over
        // any blank lines between the label and the table before removing it.
        if (from >= 0) {
          let k = out.length - 1;
          while (k >= 0 && !out[k].trim()) k--;
          if (k >= 0 && out[k].trim() === lines[from].trim()) out.splice(k, 1);
        }
        slides.push({ type: 'table', ...(title ? { title } : {}),
                      data: { headers, rows } });
        marks.push({ at: out.length, title });
        i = j - 1;              // skip the consumed table
        continue;
      }
    }
    out.push(line);
  }

  // Slides always render after the text, so a table lifted from the middle
  // of a message ends up below prose that discussed it. Leave a signpost --
  // but only when something actually follows, since a table at the very end
  // needs no explanation.
  for (let k = marks.length - 1; k >= 0; k--) {
    const { at, title } = marks[k];
    const followed = out.slice(at).some((l) => l.trim());
    if (!followed) continue;
    const label = title ? `*${title}*` : 'table';
    const which = slides.length > 1 ? ` (${k + 1} of ${slides.length})` : '';
    out.splice(at, 0, `_↓ ${label}${which} is shown below_`);
  }

  // Collapse the blank space a lifted table leaves behind.
  let text = out.join('\n').replace(/\n{3,}/g, '\n\n').trim();

  // A message that was nothing but a label and a table leaves no prose at
  // all. Cliq needs some text, and the label is the honest thing to use.
  if (!text && slides.length) {
    text = slides[0].title || 'Details';
  }
  return { text, slides };
}

module.exports = { splitTables, toCliqMarkdown, splitRow, normaliseHeaders,
  splitForCliq, MAX_PART, MAX_PARTS,
                   MAX_SLIDES, MAX_ROWS };
