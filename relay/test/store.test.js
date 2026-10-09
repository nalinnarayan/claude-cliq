'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { createStore, lit, num } = require('../functions/relay/store-datastore.js');

test('lit escapes embedded quotes', () => {
  assert.strictEqual(lit("abc"), "'abc'");
  assert.strictEqual(lit("o'brien"), "'o''brien'");
  assert.strictEqual(lit("' OR 1=1 --"), "''' OR 1=1 --'");
});

test('num refuses anything that is not a finite number', () => {
  assert.strictEqual(num(12.5), '12.5');
  for (const bad of ['1; DROP TABLE Pending', NaN, Infinity, 'abc', {}]) {
    assert.throws(() => num(bad), /not a number/);
  }
});

/** Captures the SQL a store call generates, without running it. */
function spyApp() {
  const queries = [];
  const writes = [];
  const table = (name) => ({
    insertRow: async (r) => { writes.push(['insert', name, r]); return { ROWID: '1' }; },
    updateRow: async (r) => { writes.push(['update', name, r]); },
    deleteRow: async (id) => { writes.push(['delete', name, id]); },
  });
  return {
    queries, writes,
    zcql: () => ({ executeZCQLQuery: async (sql) => { queries.push(sql); return []; } }),
    datastore: () => ({ table }),
  };
}

test('a hostile token hash cannot break out of the query', async () => {
  const app = spyApp();
  const store = createStore(app);
  await store.getDevice("x' OR '1'='1");
  const sql = app.queries[0];
  assert.match(sql, /token_hash = 'x'' OR ''1''=''1'/);
  // One quoted literal, so no stray boolean got spliced into the WHERE.
  assert.strictEqual((sql.match(/'/g) || []).length % 2, 0);
});

test('a hostile cliq_user cannot break out of listPending', async () => {
  const app = spyApp();
  const store = createStore(app);
  await store.listPending("u1' OR 1=1 --", 0);
  const sql = app.queries[0];
  assert.match(sql, /cliq_user = 'u1'' OR 1=1 --'/);
  assert.doesNotMatch(sql, /WHERE cliq_user = 'u1' OR 1=1/);
});

test('listPending always bounds by time as well as user', async () => {
  const app = spyApp();
  await createStore(app).listPending('u1', 0);
  assert.match(app.queries[0], /ts > \d/);   // never an unbounded scan
});

test('message bodies are written to the body column', async () => {
  const app = spyApp();
  await createStore(app).putPending({
    id: 'm1', cliq_user: 'u1', text: 'hello', ts: 123, expires: 456000, retain: false,
  });
  const [kind, table, row] = app.writes[0];
  assert.strictEqual(kind, 'insert');
  assert.strictEqual(table, 'Pending');
  assert.strictEqual(row.body, 'hello');
  assert.strictEqual(row.retain, false);
});

test('deletePending removes exactly the delivered rows', async () => {
  const app = spyApp();
  await createStore(app).deletePending([{ ROWID: 'a' }, { ROWID: 'b' }, { nope: 1 }]);
  assert.deepStrictEqual(app.writes.map((w) => w[2]), ['a', 'b']);
});
