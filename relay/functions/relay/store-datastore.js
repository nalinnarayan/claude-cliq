'use strict';
/**
 * Catalyst Data Store backing for the relay.
 *
 * Writes go through the SDK's insertRow/updateRow/deleteRow, which
 * parameterise values properly. Reads need ZCQL, since Data Store can only
 * fetch directly by ROWID -- so every value interpolated into a query goes
 * through `lit()` or `num()` below, and nothing else is ever concatenated in.
 *
 * Tables and columns are listed in relay/schema.json.
 */

/** Single-quote a string for ZCQL, doubling any quote it contains. */
function lit(value) {
  return `'${String(value).replace(/'/g, "''")}'`;
}

/** Only ever emit a finite number into a query. */
function num(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) throw new Error(`not a number: ${value}`);
  return String(n);
}

/** ZCQL hands back [{TableName: {...cols, ROWID}}]; flatten that. */
function rows(result, table) {
  if (!Array.isArray(result)) return [];
  return result.map((r) => r?.[table] ?? r).filter(Boolean);
}

function createStore(app) {
  const zcql = app.zcql();
  const ds = app.datastore();
  const query = async (sql, table) => rows(await zcql.executeZCQLQuery(sql), table);
  const first = async (sql, table) => (await query(sql, table))[0] || null;

  return {
    // -- pairing codes ---------------------------------------------------
    async putPairCode(code, v) {
      await ds.table('PairCodes').insertRow({
        code, cliq_user: v.cliq_user, email: v.email, expires: v.expires,
      });
    },
    getPairCode(code) {
      return first(`SELECT ROWID, code, cliq_user, email, expires
                    FROM PairCodes WHERE code = ${lit(code)}`, 'PairCodes');
    },
    async deletePairCode(code) {
      const row = await first(`SELECT ROWID FROM PairCodes WHERE code = ${lit(code)}`,
                              'PairCodes');
      if (row) await ds.table('PairCodes').deleteRow(row.ROWID);
    },

    // -- devices ---------------------------------------------------------
    async putDevice(hash, v) {
      await ds.table('Devices').insertRow({
        token_hash: hash, cliq_user: v.cliq_user, email: v.email,
        name: v.name, created: v.created,
      });
    },
    getDevice(hash) {
      return first(`SELECT ROWID, token_hash, cliq_user, email, name
                    FROM Devices WHERE token_hash = ${lit(hash)}`, 'Devices');
    },
    async deleteDevice(hash) {
      const row = await first(`SELECT ROWID FROM Devices WHERE token_hash = ${lit(hash)}`,
                              'Devices');
      if (row) await ds.table('Devices').deleteRow(row.ROWID);
    },

    // -- pending replies (the hot path) ----------------------------------
    async putPending(m) {
      await ds.table('Pending').insertRow({
        msg_id: m.id, cliq_user: m.cliq_user,
        // Bodies are kept only for the debug allowlist; for everyone else
        // the row carries just enough to deliver it, and is deleted on read.
        body: m.text, ts: m.ts, expires: Math.floor(m.expires / 1000),
        retain: !!m.retain,
      });
    },

    async listPending(cliqUser, since) {
      // The TTL job only sweeps daily, so bound by time here too rather
      // than trusting expiry to have happened.
      const cutoff = Date.now() / 1000 - 24 * 3600;
      const floor = Math.max(Number(since) || 0, cutoff);
      const found = await query(
        `SELECT ROWID, msg_id, cliq_user, body, ts FROM Pending
         WHERE cliq_user = ${lit(cliqUser)} AND ts > ${num(floor)}
         ORDER BY ts LIMIT 25`, 'Pending');
      return found.map((r) => ({
        ROWID: r.ROWID, id: r.msg_id, cliq_user: r.cliq_user,
        text: r.body, ts: Number(r.ts),
      }));
    },

    async deletePending(delivered) {
      const table = ds.table('Pending');
      for (const row of delivered) {
        if (row?.ROWID) await table.deleteRow(row.ROWID);
      }
    },

    // -- last-notified session, for the late-reply path -------------------
    async putSession(cliqUser, s) {
      const table = ds.table('Sessions');
      const existing = await first(
        `SELECT ROWID FROM Sessions WHERE cliq_user = ${lit(cliqUser)}`, 'Sessions');
      const payload = {
        cliq_user: cliqUser, session_id: s.session_id,
        cwd: s.cwd, request_id: s.request_id, ts: s.ts,
      };
      if (existing) await table.updateRow({ ROWID: existing.ROWID, ...payload });
      else await table.insertRow(payload);
    },

    // -- send budget ------------------------------------------------------
    async bumpCounter(bucket) {
      const table = ds.table('Counters');
      const cur = await first(
        `SELECT ROWID, n FROM Counters WHERE bucket = ${lit(bucket)}`, 'Counters');
      const n = (Number(cur?.n) || 0) + 1;
      // A read-modify-write can undercount under concurrency, which is
      // exactly why the budget is 40 against Cliq's real ceiling of 50.
      if (cur) await table.updateRow({ ROWID: cur.ROWID, n });
      else await table.insertRow({ bucket, n });
      return n;
    },
  };
}

module.exports = { createStore, lit, num, rows };
