# Creating the Data Store tables by hand

Catalyst console → your project (**<project>**) → **Cloud Scale →
Data Store** → **New Table**.

Every table already has `ROWID`, `CREATORID`, `CREATEDTIME` and
`MODIFIEDTIME` — Catalyst's system columns. Leave them; the relay deletes
delivered rows by `ROWID`.

Leave **Search Index** and **PII/ePHI** off everywhere, and leave table
scope and permissions at their defaults.

Five tables, 22 columns. Generated from `relay/schema.json` by
`docs/gen_tables.py` — regenerate rather than editing this by hand.

## Devices

_One row per paired laptop. The token hash is the only thing that maps a request to a Cliq identity._

| Column | Data Type | Size | Toggles | Why |
| --- | --- | --- | --- | --- |
| `token_hash` | varchar | Max Length **64** | **Mandatory**, **Unique** | SHA-256 of the device token; the raw token is never stored |
| `cliq_user` | varchar | Max Length **64** | **Mandatory** | Cliq user id this device belongs to |
| `email` | varchar | Max Length **255** | **Mandatory** | Address the bot DMs |
| `name` | varchar | Max Length **100** | — | Hostname, so a user can tell their machines apart |
| `created` | bigint | no size field | — | Epoch ms when paired |

## PairCodes

_Short-lived one-time codes issued by the bot's welcome handler._

| Column | Data Type | Size | Toggles | Why |
| --- | --- | --- | --- | --- |
| `code` | varchar | Max Length **32** | **Mandatory**, **Unique** | The code typed into /claude-cliq:pair |
| `cliq_user` | varchar | Max Length **64** | **Mandatory** |  |
| `email` | varchar | Max Length **255** | **Mandatory** |  |
| `expires` | bigint | no size field | **Mandatory** | Epoch ms; codes are also deleted on first use |

## Pending

_Replies waiting to be collected by a long-poll. The hot path -- rows are deleted as soon as they are delivered._

| Column | Data Type | Size | Toggles | Why |
| --- | --- | --- | --- | --- |
| `msg_id` | varchar | Max Length **100** | **Mandatory**, **Unique** | Cliq message id; unique makes redelivery idempotent |
| `cliq_user` | varchar | Max Length **64** | **Mandatory** | Owner; every read filters on this |
| `body` | text | no size field | — | Reply text, deleted on delivery unless the user is on the debug allowlist |
| `ts` | double | no size field | **Mandatory** | Epoch seconds; the long-poll cursor |
| `expires` | bigint | no size field | — |  |
| `retain` | boolean | no size field | — | True only for debug-allowlisted users |

## Sessions

_The last session each user was notified about, so a late reply knows what to offer to resume._

| Column | Data Type | Size | Toggles | Why |
| --- | --- | --- | --- | --- |
| `cliq_user` | varchar | Max Length **64** | **Mandatory**, **Unique** |  |
| `session_id` | varchar | Max Length **64** | **Mandatory** |  |
| `cwd` | text | no size field | — | Working directory; Text not varchar because a truncated path would break --resume |
| `request_id` | varchar | Max Length **16** | — |  |
| `ts` | double | no size field | — |  |

## Counters

_Per-minute send budget, guarding Cliq's shared 50/min ceiling._

| Column | Data Type | Size | Toggles | Why |
| --- | --- | --- | --- | --- |
| `bucket` | varchar | Max Length **64** | **Mandatory**, **Unique** | e.g. send:29193847 |
| `n` | int | no size field | **Mandatory** |  |

## Two things that are easy to get wrong

**`cliq_user` appears in four tables with different flags.** It is Unique
only in `Sessions` (one row per person, overwritten). In `Devices`,
`PairCodes` and `Pending` it must NOT be unique — one person pairs several
machines and queues several replies.

**`Pending.ts` must be Double, not bigint.** It is the long-poll cursor and
carries fractional epoch seconds. A bigint accepts the write, silently
truncates, and replies then arrive late or twice. The console shows no
decimal-digits field for Double — that is expected, it is floating point.

## Verifying them

Once the relay is deployed, make it prove itself rather than eyeballing:

```sh
curl -s -X POST "$RELAY_URL/selftest" \
  -H "X-Relay-Secret: $RELAY_INBOUND_SECRET" | python3 -m json.tool
```

It writes a probe row to each table, reads it back through ZCQL, checks the
values survived, and deletes it. Healthy:

```json
{ "ok": true, "summary": "all 5 tables are good", "results": [...] }
```

Anything mistyped comes back named:

```json
{ "table": "Pending", "ok": false,
  "error": "ts lost precision (sent 1791548096.42, got 1791548096) — is it a double?" }
```

Reading back is the point: a column of the wrong type will often accept the
write and only betray itself on the way out.
