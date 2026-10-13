# claude-cliq

Get pinged in Zoho Cliq when Claude Code finishes, asks you something, or needs
permission — and reply from Cliq to keep it working.

Running a long task today means babysitting the terminal: you either watch it,
or check back every few minutes to see whether Claude stopped early. This makes
the terminal stop being the only place a session lives.

```
Claude finishes  ──▶  Cliq DM  ──▶  you reply "add tests too"  ──▶  Claude continues
Claude needs sudo ──▶ Cliq DM  ──▶  you reply "yes abcde"      ──▶  the command runs
```

Reply within the wait window and work continues in your terminal. The window is
30 minutes, restarting on every reply, for up to 100 replies in a row. Reply
after that and the bot tells you where the work is — a released session can
only be picked up at the terminal.

## For a teammate: install

1. Subscribe to the **Claude Code** bot in Cliq (sidebar `+` → Bots, or open
   `https://cliq.zoho.com/bots/<bot_unique_name>`). It DMs you a pairing code.
2. In your terminal:
   ```sh
   claude plugin marketplace add <this-repo>
   claude plugin install claude-cliq
   ```
3. Pair this machine, using the code from the DM:
   ```
   /claude-cliq:pair <code>
   ```
   Pairing also raises `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` in your
   `~/.claude/settings.json`, and tells you it has. Claude Code stops
   honouring a Stop-hook block after 8 consecutive ones with no tool call
   between — a guard against hooks that block unconditionally. This one waits
   on a human every time, so it cannot spin, and without the raise a long
   back-and-forth from Cliq gets cut off. **Restart Claude Code afterwards**
   for it to apply.
4. When you step away:
   ```
   /claude-cliq:away on
   ```

No OAuth app, no API console, no tokens to mint. Your laptop never holds a Cliq
credential — only a device token for the relay.

## Using it

**Away off** (the default) — every trigger still DMs you, nothing ever blocks,
and the terminal behaves exactly as it would without this installed.

**Away on** — Claude waits for your reply before finishing a turn or acting on a
permission request.

Replying:

| You type | What happens |
| --- | --- |
| `add tests too` | Fed back to Claude as your next instruction |
| `yes abcde` / `no abcde` | Approves or denies permission request `abcde` |
| `all abcde` | Approves **everything in that session** for 30 minutes |
| `abcde use postgres` | Answers a specific session when several are waiting |

`all` exists because a session doing unfamiliar work can ask six times in two
minutes, and answering each from a phone is miserable. It is deliberately
bounded — one session, half an hour, and revoked the moment you run
`/claude-cliq:away off`. The bot tells you in the chat exactly what it granted,
because a blanket approval you have forgotten about is the dangerous kind.

A bare reply works whenever only one thing is waiting. When several are, the bot
asks you to prefix an id.

Permission requests that go unanswered fall back to the normal terminal prompt —
they are never auto-denied.

## What triggers a ping

| Trigger | Hook | Blocks? |
| --- | --- | --- |
| Task finished / Claude asked something | `Stop` | only when away |
| Permission request, plan approval, next phase | `PermissionRequest` | only when away |
| A tool failed | `PostToolUseFailure` | never |
| Session idle and waiting on you | `Notification` (`idle_prompt`) | never |

## For the maintainer: one-time setup

### 1. Cliq bot — start private

Cliq → **Bots & Tools → Bots → Create**. **Leave Access Level at Private**,
which is the default. Note the `unique_name` from the bot's API endpoint URL.

Private means only you can see or subscribe to it, so the whole loop can be
exercised without anyone else encountering a half-built bot. Flipping it to
**Organization** later is a single setting and needs no code change — the relay
never cares about visibility, only about who holds a device token.

Rolling out in two phases:

| | Private (now) | Organization (later) |
| --- | --- | --- |
| Who can subscribe | just you | anyone in the org |
| `DEBUG_USERS` | your Cliq user id | same — body logging stays yours alone |
| Relay / plugin code | unchanged | unchanged |

Before flipping, make sure the pieces that only matter with real users are in
place: the send budget guards Cliq's shared 50/min ceiling, and bodies are
stored only for ids in `DEBUG_USERS`.

Generate the two handlers with your real relay URL and secret already in them:

```sh
python3 cliq-extension/render.py
```

That writes `cliq-extension/generated/`, which is untracked because the
handlers carry `RELAY_INBOUND_SECRET`. Paste each into its handler in the bot
builder — `welcome_handler.dg` into the Welcome handler, `message_handler.dg`
into the Message handler.

There are no permissions to configure. That is a webhook-execution-bot concept,
where Cliq has to be told what to put in the HTTP payload; a Deluge bot gets
`user`, `message` and `chat` as variables automatically. Cliq instead shows each
user a consent dialog on first execution, derived from what the code touches —
which is why the handlers stay minimal, and why that consent **lapses every 60
days**.

### 2. Webhook token

Cliq → **Bots & Tools → Webhook Tokens** (2FA-gated; the session expires after
15 minutes, max 5 tokens per user). This single token is the only Cliq
credential the whole deployment needs.

### 3. Catalyst

A new dedicated project. Create the five Data Store tables defined in
`relay/schema.json` — the column objects there are in exactly the shape the
Catalyst MCP's `Create_Column` tool expects, so they can be applied directly or
copied into the console:

| Table | Holds |
| --- | --- |
| `Devices` | one row per paired laptop, keyed by SHA-256 of its token |
| `PairCodes` | short-lived one-time codes from the welcome handler |
| `Pending` | replies waiting for a long-poll; deleted on delivery |
| `Sessions` | the last session each user was pinged about |
| `Counters` | the per-minute send budget |

Data Store rather than NoSQL for three reasons: the queries are plain ZCQL and
far easier to verify, rows are inspectable in the console when someone reports a
lost reply, and the Catalyst MCP can create the tables (it has no NoSQL tools at
all). The trade is cost — Data Store reads are ~200x NoSQL — which is why the
long-poll re-checks storage every 3s rather than every 400ms. That is
imperceptible to someone who just typed a message and cuts reads per poll leg
about sevenfold. Tune with `POLL_TICK_MS` if needed.

Set these environment variables on the function:

```
CLIQ_BOT_NAME          the bot's unique_name
CLIQ_ZAPIKEY           the webhook token from step 2
CLIQ_BASE_URL          https://cliq.zoho.com   (or .in for that data centre)
RELAY_INBOUND_SECRET   a long random string, also pasted into both handlers
DEBUG_USERS            your own Cliq user id, comma separated
```

Catalyst env vars are **not documented as encrypted at rest** — treat them as
config, and rotate the zapikey if the project is ever shared.

Then bind the directory to your project and deploy:

```sh
cd relay && npm install
catalyst login                                  # once, for the right account
catalyst project:use <project_id> --org <org_id>
cp .env.deploy.example .env.deploy              # fill in, then:
./deploy.sh
```

`.catalystrc` is deliberately untracked — it pins one project and org, so
everyone generates their own with `project:use`. `deploy.sh` merges
`.env.deploy` into the function config just before deploying and strips it
straight after, so secrets never reach a tracked file.

Set function memory to **128 MB** (the default of 256 doubles the bill for
I/O-bound work).

### 4. Publish the extension

Cliq → **Bots & Tools → Extensions → Publish**, and do **not** tick the
Marketplace checkbox. That gives a private install link for your org and skips
Zoho's review entirely. An org admin installs it once; after that anyone can
subscribe.

Existing staff must subscribe themselves — Cliq's default-bot assignment only
fires when someone joins the org, and there is no bulk-subscribe API.

## Local development

The whole laptop side runs against a mock relay with no Zoho account at all:

```sh
python3 scripts/mockrelay.py &                       # prints "DMs" to the console
CODE=$(curl -s localhost:8899/newcode | python3 -c 'import json,sys;print(json.load(sys.stdin)["code"])')
CLAUDE_CLIQ_HOME=/tmp/cliqtest python3 scripts/cliqctl.py pair $CODE --relay http://127.0.0.1:8899
CLAUDE_CLIQ_HOME=/tmp/cliqtest python3 scripts/cliqctl.py away on
python3 scripts/mockrelay.py --say "your reply"      # act as the user in Cliq
```

Relay logic has its own tests, including the cross-user isolation checks:

```sh
cd relay && node --test test/core.test.js
```

## How isolation works

The bot is discoverable org-wide, so this is the part that matters.

- **Identity comes from Cliq, never from the laptop.** Cliq invokes the handler
  and supplies the sender's user id server-side.
- **Authorization is derived from the credential, never from a request
  parameter.** `/await` takes only the device token and the relay resolves the
  owner itself; `/notify` derives the recipient the same way, so a laptop never
  names a recipient and cannot address someone else's DM. Both properties have
  tests that try to break them, and both were re-checked against the deployed
  relay: a second user asking for the first user's messages by name gets only
  their own.
- The device token travels in **`X-Device-Token`**, not `Authorization` —
  Catalyst's gateway claims that header and rejects anything that is not one of
  its own OAuth tokens.
- Device tokens are 256-bit random and stored only as SHA-256 hashes.
- Message bodies are retained only for Cliq user ids in `DEBUG_USERS`;
  everyone else's are deleted on delivery.

A stolen device token would let someone read that one user's pings. The config
file is `0600` and `/claude-cliq:pair` revokes any previous token.

## Known limits

- **50 bot messages/min is a global ceiling**, shared across every user, because
  the relay posts under one token identity. The relay budgets 40 and sheds the
  rest — breaching it locks everyone out for 10 minutes.
- **Deluge consent expires every 60 days** per user. It presents as the bot
  going quiet; re-subscribing fixes it.
- **A released session cannot be resumed from Cliq.** An interactive session
  sitting at its prompt takes input from its terminal and nowhere else, so once
  the wait window or the 100-reply ceiling is reached, the work is picked up
  there. The bot says so rather than pretending to deliver.
- Cliq does not redeliver a failed handler call, so a dropped reply is dropped.

## Layout

```
scripts/      laptop side: hooks, daemon, CLI, mock relay   (python3 stdlib only)
relay/        Catalyst Advanced I/O function + tests        (node)
cliq-extension/  the two Deluge handlers
hooks/        hook registrations
skills/       /claude-cliq:pair and :away
```
