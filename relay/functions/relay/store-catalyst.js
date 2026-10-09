'use strict';
/**
 * Catalyst NoSQL backing for the relay.
 *
 * NoSQL rather than Cache or Data Store because the long-poll inner loop is
 * the hot path: Cache GETs cost ~133x and Data Store SELECTs ~200x the same
 * read here.
 *
 * Tables (create these once, see relay/README.md):
 *   Devices    partition token_hash
 *   PairCodes  partition code
 *   Pending    partition cliq_user, sort  ts_id
 *   Sessions   partition cliq_user
 *   Counters   partition bucket
 */

const { NoSQLMarshall: M } = require('zcatalyst-sdk-node/lib/no-sql/marshall');

const S = (v) => M.makeString(String(v));
const N = (v) => M.makeNumber(Number(v));
const B = (v) => M.makeBoolean(Boolean(v));

/** Sort key that makes "everything since T" a range scan. */
const tsKey = (ts, id) => `${String(Math.floor(ts * 1000)).padStart(16, '0')}#${id}`;

function plain(item) {
  // Unwrap one level of {S:…}/{N:…}/{BOOL:…} into ordinary JS values.
  const out = {};
  for (const [k, v] of Object.entries(item || {})) {
    if (v == null || typeof v !== 'object') { out[k] = v; continue; }
    if ('S' in v) out[k] = v.S;
    else if ('N' in v) out[k] = Number(v.N);
    else if ('BOOL' in v) out[k] = v.BOOL;
    else out[k] = v;
  }
  return out;
}

function createStore(app) {
  const nosql = app.nosql();
  const tables = new Map();
  const table = async (name) => {
    if (!tables.has(name)) tables.set(name, await nosql.getTable(name));
    return tables.get(name);
  };

  const fetchOne = async (name, keys) => {
    const t = await table(name);
    const res = await t.fetchItem({ keys });
    const item = res?.get?.('item') ?? res?.item;
    const first = Array.isArray(item) ? item[0] : item;
    return first ? plain(first) : null;
  };

  return {
    async putPairCode(code, v) {
      const t = await table('PairCodes');
      await t.insertItems({ item: {
        code: S(code), cliq_user: S(v.cliq_user), email: S(v.email),
        expires: N(v.expires),
      } });
    },
    getPairCode: (code) => fetchOne('PairCodes', { code: S(code) }),
    async deletePairCode(code) {
      const t = await table('PairCodes');
      await t.deleteItems({ keys: { code: S(code) } });
    },

    async putDevice(hash, v) {
      const t = await table('Devices');
      await t.insertItems({ item: {
        token_hash: S(hash), cliq_user: S(v.cliq_user), email: S(v.email),
        name: S(v.name), created: N(v.created),
      } });
    },
    getDevice: (hash) => fetchOne('Devices', { token_hash: S(hash) }),
    async deleteDevice(hash) {
      const t = await table('Devices');
      await t.deleteItems({ keys: { token_hash: S(hash) } });
    },

    async putPending(m) {
      const t = await table('Pending');
      await t.insertItems({ item: {
        cliq_user: S(m.cliq_user), ts_id: S(tsKey(m.ts, m.id)),
        id: S(m.id), text: S(m.text), ts: N(m.ts),
        expires: N(Math.floor(m.expires / 1000)), retain: B(m.retain),
      } });
    },

    async listPending(cliqUser, since) {
      const t = await table('Pending');
      const res = await t.queryTable({
        key_condition: {
          group_operator: 'AND',
          group: [
            { attribute: 'cliq_user', operator: 'equals', value: S(cliqUser) },
            { attribute: 'ts_id', operator: 'greater_than',
              value: S(tsKey(since || 0, '')) },
          ],
        },
        forward_scan: true,
        limit: 25,
      });
      const items = res?.get?.('item') ?? res?.item ?? [];
      // Belt and braces: the TTL sweeper only runs daily, so filter on time
      // at read time rather than trusting expiry to have happened.
      const cutoff = Date.now() / 1000 - 24 * 3600;
      return (Array.isArray(items) ? items : [items])
        .filter(Boolean).map(plain)
        .filter((m) => m.cliq_user === cliqUser && m.ts > (since || 0) && m.ts > cutoff);
    },

    async deletePending(rows) {
      const t = await table('Pending');
      for (const row of rows) {
        if (!row || !row.cliq_user || !row.ts_id) continue;
        await t.deleteItems({ keys: {
          cliq_user: S(row.cliq_user), ts_id: S(row.ts_id),
        } });
      }
    },

    async putSession(cliqUser, s) {
      const t = await table('Sessions');
      await t.insertItems({ item: {
        cliq_user: S(cliqUser), session_id: S(s.session_id),
        cwd: S(s.cwd), request_id: S(s.request_id), ts: N(s.ts),
      } });
    },

    async bumpCounter(bucket) {
      const t = await table('Counters');
      const cur = await fetchOne('Counters', { bucket: S(bucket) });
      const n = (cur?.n || 0) + 1;
      // A read-modify-write can undercount under concurrency; we budget 40
      // against Cliq's 50 precisely so a small race stays harmless.
      await t.insertItems({ item: { bucket: S(bucket), n: N(n) } });
      return n;
    },
  };
}

module.exports = { createStore, tsKey, plain };
