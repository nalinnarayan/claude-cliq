'use strict';
/**
 * Round-trips every table so a mistyped column is caught at setup rather
 * than as a confusing runtime failure weeks later.
 *
 * Inserts a probe row, reads it back through ZCQL, deletes it. Reading back
 * is the point: a column of the wrong type will often accept a write and
 * then return something unusable.
 */

const PROBES = [
  {
    table: 'Devices',
    row: () => ({
      token_hash: `selftest-${Date.now()}`, cliq_user: 'selftest',
      email: 'selftest@example.com', name: 'selftest', created: Date.now(),
    }),
    key: 'token_hash',
    check: (got, sent) => got.cliq_user === sent.cliq_user,
  },
  {
    table: 'PairCodes',
    row: () => ({
      code: `st${Date.now()}`, cliq_user: 'selftest',
      email: 'selftest@example.com', expires: Date.now() + 60000,
    }),
    key: 'code',
    check: (got, sent) => String(got.expires) === String(sent.expires),
  },
  {
    table: 'Pending',
    row: () => ({
      msg_id: `selftest-${Date.now()}`, cliq_user: 'selftest',
      body: 'selftest body', ts: Date.now(),
      expires: Date.now() + 60000, retain: false,
    }),
    key: 'msg_id',
    // The fractional part is the whole reason ts must be a double; an
    // integer column truncates it and the long-poll cursor misbehaves.
    check: (got, sent) => {
      if (got.body !== sent.body) return 'body did not round-trip';
      // Must survive exactly. Catalyst's double mangles a 10-digit epoch, so
      // this column has to be bigint.
      if (Number(got.ts) !== sent.ts) {
        return `ts came back as ${got.ts}, sent ${sent.ts} — must be bigint, not double`;
      }
      return true;
    },
  },
];

function lit(v) { return `'${String(v).replace(/'/g, "''")}'`; }

async function runSelftest(app) {
  const ds = app.datastore();
  const zcql = app.zcql();
  const results = [];

  for (const probe of PROBES) {
    const sent = probe.row();
    let rowid = null;
    try {
      const inserted = await ds.table(probe.table).insertRow(sent);
      rowid = inserted?.ROWID || inserted?.[0]?.ROWID || null;

      const found = await zcql.executeZCQLQuery(
        `SELECT * FROM ${probe.table} WHERE ${probe.key} = ${lit(sent[probe.key])}`);
      const got = (found?.[0]?.[probe.table]) || null;
      if (!got) throw new Error('wrote a row but could not read it back');
      if (!rowid) rowid = got.ROWID;

      const verdict = probe.check(got, sent);
      if (verdict !== true) {
        throw new Error(typeof verdict === 'string' ? verdict : 'value mismatch');
      }
      results.push({ table: probe.table, ok: true });
    } catch (err) {
      results.push({ table: probe.table, ok: false, error: String(err.message || err) });
    } finally {
      if (rowid) {
        try { await ds.table(probe.table).deleteRow(rowid); } catch { /* probe row */ }
      }
    }
  }

  const bad = results.filter((r) => !r.ok);
  return {
    ok: bad.length === 0,
    summary: bad.length === 0
      ? `all ${results.length} tables are good`
      : `${bad.length} of ${results.length} tables need attention`,
    results,
  };
}

module.exports = { runSelftest };
