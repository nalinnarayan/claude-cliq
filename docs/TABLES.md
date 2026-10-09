# Creating the Data Store tables by hand

Catalyst console → your project (**<project>**) → **Cloud Scale →
Data Store** → **New Table**.

Every table gets a `ROWID` column automatically — don't add one. Leave
**Search Indexing** off everywhere and leave the default table scope and
permissions alone; the console sets those up for you.

Five tables, 22 columns. Generated from `relay/schema.json` — if that file
changes, regenerate rather than editing this by hand.

## Devices

_One row per paired laptop. The token hash is the only thing that maps a request to a Cliq identity._

| Column | Type | Size | Flags | Why |
| --- | --- | --- | --- | --- |
| `token_hash` | varchar | Max length **64** | **Mandatory**, **Unique** | SHA-256 of the device token; the raw token is never stored |
| `cliq_user` | varchar | Max length **64** | **Mandatory** | Cliq user id this device belongs to |
| `email` | varchar | Max length **255** | **Mandatory** | Address the bot DMs |
| `name` | varchar | Max length **100** | — | Hostname, so a user can tell their machines apart |
| `created` | bigint | — | — | Epoch ms when paired |

## PairCodes

_Short-lived one-time codes issued by the bot's welcome handler._

| Column | Type | Size | Flags | Why |
| --- | --- | --- | --- | --- |
| `code` | varchar | Max length **32** | **Mandatory**, **Unique** | The code typed into /claude-cliq:pair |
| `cliq_user` | varchar | Max length **64** | **Mandatory** |  |
| `email` | varchar | Max length **255** | **Mandatory** |  |
| `expires` | bigint | — | **Mandatory** | Epoch ms; codes are also deleted on first use |

## Pending

_Replies waiting to be collected by a long-poll. The hot path -- rows are deleted as soon as they are delivered._

| Column | Type | Size | Flags | Why |
| --- | --- | --- | --- | --- |
| `msg_id` | varchar | Max length **100** | **Mandatory**, **Unique** | Cliq message id; unique makes redelivery idempotent |
| `cliq_user` | varchar | Max length **64** | **Mandatory** | Owner; every read filters on this |
| `body` | text | — | — | Reply text, deleted on delivery unless the user is on the debug allowlist |
| `ts` | double | Decimal digits **3** | **Mandatory** | Epoch seconds; the long-poll cursor |
| `expires` | bigint | — | — |  |
| `retain` | boolean | — | — | True only for debug-allowlisted users |

## Sessions

_The last session each user was notified about, so a late reply knows what to offer to resume._

| Column | Type | Size | Flags | Why |
| --- | --- | --- | --- | --- |
| `cliq_user` | varchar | Max length **64** | **Mandatory**, **Unique** |  |
| `session_id` | varchar | Max length **64** | **Mandatory** |  |
| `cwd` | varchar | Max length **500** | — |  |
| `request_id` | varchar | Max length **16** | — |  |
| `ts` | double | Decimal digits **3** | — |  |

## Counters

_Per-minute send budget, guarding Cliq's shared 50/min ceiling._

| Column | Type | Size | Flags | Why |
| --- | --- | --- | --- | --- |
| `bucket` | varchar | Max length **64** | **Mandatory**, **Unique** | e.g. send:29193847 |
| `n` | int | — | **Mandatory** |  |

## When you're done

Sanity-check with these counts: **5 tables**, and columns per table —
`Devices` 5, `PairCodes` 4, `Pending` 6, `Sessions` 5, `Counters` 2.

The two that matter most if something is mistyped:

- `Pending.ts` must be **double**, not bigint. It is the long-poll cursor
  and carries fractional epoch seconds; an integer column silently
  truncates and replies arrive late or twice.
- `Pending.msg_id` must be **unique**. That uniqueness is what makes a
  repeated delivery from Cliq idempotent rather than a duplicate reply.

## Verifying them

Once the relay is deployed, don't eyeball it — make it prove itself:

```sh
curl -s -X POST "$RELAY_URL/selftest" \
  -H "X-Relay-Secret: $RELAY_INBOUND_SECRET" | python3 -m json.tool
```

It writes a probe row to each table, reads it back through ZCQL, checks the
values survived, and deletes it. A healthy run:

```json
{ "ok": true, "summary": "all 5 tables are good", "results": [...] }
```

Anything mistyped comes back named, e.g.

```json
{ "table": "Pending", "ok": false,
  "error": "ts lost precision (sent 1791548096.42, got 1791548096) — is it a double?" }
```

Reading back is the point: a column of the wrong type will often accept the
write and only betray itself on the way out.
