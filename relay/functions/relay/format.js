'use strict';
/**
 * Split Markdown into what Cliq can render.
 *
 * v3 renders bold, italic, code, fences, lists, headings and links in `text`
 * on its own — but NOT tables, which arrive as raw pipes. So tables are
 * lifted out and emitted as native table slides, and everything else is left
 * alone.
 *
 * Pure: no Cliq or network imports, so the parsing is directly testable.
 */

const MAX_SLIDES = 5;          // keep a phone message readable
const MAX_ROWS = 20;           // a long table is unreadable on a phone anyway

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

/** A title for the table, taken from the line above when it reads like one. */
function titleFor(lines, start) {
  for (let i = start - 1; i >= 0 && i >= start - 2; i--) {
    const prev = (lines[i] || '').trim();
    if (!prev) continue;
    const heading = prev.match(/^#{1,6}\s+(.*)$/);
    if (heading) return heading[1].trim().slice(0, 100);
    if (prev.endsWith(':')) return prev.slice(0, -1).replace(/\*\*/g, '').trim().slice(0, 100);
    return '';
  }
  return '';
}

/**
 * @returns {{text: string, slides: Array}} prose with tables removed, plus
 *          one slide per table in document order.
 */
function splitTables(markdown) {
  const lines = String(markdown || '').split('\n');
  const out = [];
  const slides = [];
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
        const title = titleFor(lines, i);
        slides.push({ type: 'table', ...(title ? { title } : {}),
                      data: { headers, rows } });
        i = j - 1;              // skip the consumed table
        continue;
      }
    }
    out.push(line);
  }

  // Collapse the blank space a lifted table leaves behind.
  const text = out.join('\n').replace(/\n{3,}/g, '\n\n').trim();
  return { text, slides };
}

module.exports = { splitTables, splitRow, normaliseHeaders, MAX_SLIDES, MAX_ROWS };
