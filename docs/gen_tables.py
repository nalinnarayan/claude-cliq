#!/usr/bin/env python3
"""Render docs/TABLES.md from relay/schema.json so the two cannot drift."""
import json, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
s = json.load(open(ROOT / "relay" / "schema.json"))
out = []
w = out.append

w("# Creating the Data Store tables by hand\n")
w("Catalyst console → your project (**<project>**) → **Cloud Scale →")
w("Data Store** → **New Table**.\n")
w("Every table already has `ROWID`, `CREATORID`, `CREATEDTIME` and")
w("`MODIFIEDTIME` — Catalyst's system columns. Leave them; the relay deletes")
w("delivered rows by `ROWID`.\n")
w("Leave **Search Index** and **PII/ePHI** off everywhere, and leave table")
w("scope and permissions at their defaults.\n")
w("Five tables, 22 columns. Generated from `relay/schema.json` by")
w("`docs/gen_tables.py` — regenerate rather than editing this by hand.\n")

def flags(c):
    f = []
    if str(c.get("is_mandatory")).lower() == "true": f.append("**Mandatory**")
    if str(c.get("is_unique")).lower() == "true":    f.append("**Unique**")
    return ", ".join(f) or "—"

def size(c):
    t = c["data_type"]
    if t == "varchar": return f"Max Length **{c['max_length']}**"
    # The console offers no size field for these; bigint/double/boolean/text
    # each show just the type.
    return "no size field"

for t in s["tables"]:
    w(f"## {t['table_name']}\n")
    w(f"_{t['_purpose']}_\n")
    w("| Column | Data Type | Size | Toggles | Why |")
    w("| --- | --- | --- | --- | --- |")
    for c in t["columns"]:
        w(f"| `{c['column_name']}` | {c['data_type']} | {size(c)} | "
          f"{flags(c)} | {c.get('description','')} |")
    w("")

w("## Two things that are easy to get wrong\n")
w("**`cliq_user` appears in four tables with different flags.** It is Unique")
w("only in `Sessions` (one row per person, overwritten). In `Devices`,")
w("`PairCodes` and `Pending` it must NOT be unique — one person pairs several")
w("machines and queues several replies.\n")
w("**`Pending.ts` must be Double, not bigint.** It is the long-poll cursor and")
w("carries fractional epoch seconds. A bigint accepts the write, silently")
w("truncates, and replies then arrive late or twice. The console shows no")
w("decimal-digits field for Double — that is expected, it is floating point.\n")
w("## Verifying them\n")
w("Once the relay is deployed, make it prove itself rather than eyeballing:\n")
w("```sh")
w('curl -s -X POST "$RELAY_URL/selftest" \\')
w('  -H "X-Relay-Secret: $RELAY_INBOUND_SECRET" | python3 -m json.tool')
w("```\n")
w("It writes a probe row to each table, reads it back through ZCQL, checks the")
w("values survived, and deletes it. Healthy:\n")
w('```json')
w('{ "ok": true, "summary": "all 5 tables are good", "results": [...] }')
w('```\n')
w("Anything mistyped comes back named:\n")
w('```json')
w('{ "table": "Pending", "ok": false,')
w('  "error": "ts lost precision (sent 1791548096.42, got 1791548096) — is it a double?" }')
w('```\n')
w("Reading back is the point: a column of the wrong type will often accept the")
w("write and only betray itself on the way out.")

(ROOT / "docs" / "TABLES.md").write_text("\n".join(out) + "\n")
print("regenerated docs/TABLES.md")
