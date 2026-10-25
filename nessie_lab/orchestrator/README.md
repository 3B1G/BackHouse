# Orchestrator (ASI)

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

BackHouse's controller: the agent the owner talks to, on ASI:One or at the terminal. It answers questions about
cash, bills, findings, the menu and the forecast by calling tools, and it proposes fixes. It never makes a change.
Every change is a pending Suggestion file that the owner approves at their own terminal with CH (`changes approve`,
which asks for the owner passphrase).

The orchestrator is the **sole supervisor**. It delegates to two agents, the **Bookkeeper** (which runs its own
Verifier loop and returns ONE verified report) and the **Market scout**, and it uses the **Forecaster** (Monte
Carlo) and **CH** (changes: plan and dry run only) as tools.

## Ports

| Port | What | Started with |
|---|---|---|
| 8001 | Bookkeeper agent | `uv run --project bookkeeper bookkeeper serve` |
| 8002 | Bookkeeper's demo client | `uv run --project bookkeeper bookkeeper client --target agent1...` |
| 8003 | Bookkeeper Verifier agent | `uv run --project bookkeeper bookkeeper serve-verifier` (its default) |
| 8004 | Market scout's local client | the scout's own CLI |
| 8005 | **Orchestrator** (uAgent, `/ask`, `/bookkeeper`, the `/api` JSON API) | `orchestrator serve` |
| 8006 | Market scout agent | the scout's `serve` |
| 8007 | Orchestrator's local chat client (`ORCHESTRATOR_LOCAL_CLIENT_ENDPOINT` default) | your client |

Everything binds 127.0.0.1. The orchestrator's local client used to default to 8006; it is 8007 now, because 8006 is
the Market scout's.

## How a turn works

1. **Stage 1, "what is going on?"**: `get_cash_position`, `list_bills_due`, `lookup` (the Bookkeeper's read-only
   tools), `get_findings`, `scan_market`, `get_menu`, `list_upcoming_changes`, `forecast_base`, `forecast_scenario`,
   `bookkeeper_status` (read-only).
2. **Stage 2, "what do we do about it?"**: `propose_fix` writes a Suggestion and returns its `approve_command`;
   `preview_suggestion` re-runs CH's dry-run plan for it.

`turn(session, text)` (`src/orchestrator/turn.py`):

- "How are we doing", "status", "summary", "brief" are answered from `brief(session)` directly, with no LLM: it is
  fast and deterministic.
- Anything else goes to ASI:One (`llm.py`): the OpenAI-compatible chat completions API with `tools`. Each tool call is
  run with `tools.call`, and its result goes back as a `role: "tool"` message with the same `tool_call_id`. A turn
  makes at most 6 model requests within 60 seconds. The last request is sent without tools, so the model has to answer.
  Only HTTP 429/500/502/503 are retried (3 times, backoff with jitter); a 402 or any other 4xx is a clean error.
- **Figure guard** (`guard.py`): the answer is normalised first (NFKC, zero-width and other format characters
  dropped, one kind of dash, ASCII digits), and that exact string is guarded and sent. Every money figure
  (`$1,234`, `$ 21`, `48,000 USD`, `USD 5,000`, `$1.2 million`, "twelve thousand dollars"), date (`2026-10-30`,
  `10/30/2026`, `Oct 31`, `October 31, 2026`) and percent (`14%`, `14 pct`, `14 percent`) must appear in a
  successful tool result in `session.log`. Cents are normalised to dollars, and "$21" matches 21.00. Rounding
  follows the answer: "$21" matches 20.75, "$21.00" does not. If a figure has no match, the model gets one request
  to fix it. If it still has no match, the sentences with unsupported figures are cut and a "Figures I could
  verify:" list is appended. Any sentence claiming a change was applied is cut the same way.
- If a suggestion was proposed, the reply carries its approve command, whatever the model wrote, and every reply
  ends with the fixed line "Nothing has been changed; approve at your terminal to apply."
- With no key, a failed request or any loop error, the reply is the brief, with `degraded: true` and a note.
- Card clicks (`{"action": "save"|"discard", "suggestion": path}`): **save** confirms the pending file and repeats
  its approve command. **discard** drops it from this chat. Neither approves. Only paths this chat's `propose_fix`
  returned are recognised.

## CLI

From `nessie_lab/`:

```bash
python -m uv sync --project orchestrator
python -m uv run --project orchestrator orchestrator brief
python -m uv run --project orchestrator orchestrator ask "What should I change on the menu?"
python -m uv run --project orchestrator orchestrator serve          # the uAgent, local mode, 127.0.0.1:8005
python -m uv run --project orchestrator pytest -q orchestrator/tests
```

`POST http://127.0.0.1:8005/ask` with `{"text": "...", "session_id": "demo", "reset": false}` and the header
`Authorization: Bearer <data/orchestrator/rest_token>` runs the same turn and returns
`{"text", "suggestions", "degraded", "notes", "status"}`. `serve` writes a fresh token at every start (owner-only)
and prints it once; `orchestrator ask --server "..."` reads it from the file. `/ask` is registered only when the
server is bound to a loopback address.

## Bookkeeper: one-time setup, then quick updates

The Bookkeeper (bk branch, `nessie_lab/bookkeeper`) reviews the whole bank window in one run, and since 872f15e
every run is drafted by the Bookkeeper and checked by its own Verifier: two investigations, ~40 ASI calls plus
revision rounds, up to an hour. The orchestrator runs that long review ONCE, then keeps a merged view of every report it has
ingested in `data/orchestrator/bookkeeper_state.json`, so updates only need the newest days and the earlier findings
stay in context. These are the owner's commands; no LLM tool can start a Bookkeeper run.

```bash
python -m uv run --project orchestrator orchestrator bookkeeper-setup     # once: the full, verified backfill (--timeout 3600, --force to redo, --agents)
python -m uv run --project orchestrator orchestrator bookkeeper-update    # later: only what is new (--timeout 1200, --agents)
python -m uv run --project orchestrator orchestrator bookkeeper-status    # baseline, covered through, days behind, counts, last run
python -m uv run --project orchestrator orchestrator bookkeeper-ingest data/reports/R-<id>.json   # a report made by hand
```

- **How it runs the Bookkeeper.** As a subprocess, from `nessie_lab/`:
  `uv run --project bookkeeper bookkeeper --root <root> run --request-id orch-bk-<as_of>-<8hex> --as-of <date>
  --tag <tag>`. Since bk 872f15e one run is **two investigations**: the Bookkeeper drafts, its Verifier repeats the
  investigation independently, sends disagreements back for revision rounds and publishes one verified report
  (~40 ASI calls plus revisions). So the timeouts are 3600 s for setup and 1200 s for an update. The CLI prints
  `{request_id, report_id, filename, as_of, verified}` (no `status` key); the orchestrator reads `filename`
  (pattern-checked) and `verified`. `as_of` is clamped to the latest bank date (`MAX(date)` of `bank_tx`). You get a
  clear error when `nessie_lab/bookkeeper` is missing (merge the bk branch, or use `bookkeeper-ingest`), when
  `ASI1_API_KEY` is missing, or on a timeout (the message names the `R-<sha>.json` it may still write, and
  `bookkeeper verify --report-id R-<sha>` for a draft that was saved but not verified). A transport failure with a
  trace id is retried once with `bookkeeper resume`, which now verifies too, so the filename it prints is final.
  `ORCHESTRATOR_BK_UV` overrides the uv command. With `--agents`, setup and update go through the running `serve`
  (`POST /bookkeeper`), which delegates to the Bookkeeper agent (below).
- **Merging.** Finding ids are renumbered on every run, so findings are matched by a key: the Bookkeeper's own
  `finding_key` when it sends one, else `sha256(root_cause | counterparty | treatment.kind | target)`, and failing
  that, the same root cause with overlapping bank ids. A FULL report replaces the open set: a finding it no longer
  lists becomes `not_rereported` (kept, with a note, never used in the forecast). A DELTA report adds and updates
  findings, and closes the ones it marks `resolved` or `superseded`. `covered_through` moves to the report's window
  end. Re-ingesting a report is a no-op; a report with the same name and different content is refused; a delta that
  leaves a gap, or a full report older than the state, is refused.
- **What reads it.** `get_findings` and the forecasts use only the OPEN findings, through a synthetic schema-1.1
  report (`bk_state.open_findings_report`, ids `key[:10]` so they can't collide) -> `from_bk_report` ->
  `checks_bk` (no owner decisions yet: every finding awaits the owner) -> `passed_adjustments`. `get_findings` adds
  `coverage` and a "days behind" note. With no state file, the latest `data/reports/R-*.json` is used directly.
- **Updates.** `bookkeeper-update` says "up to date" when `covered_through` is the latest bank date. Otherwise,
  with `ORCHESTRATOR_BK_INCREMENTAL=1`, it asks for a delta since `covered_through + 1` with `prior_report_id`;
  without it, it runs a full rerun at the latest date and diffs it against the state.
- **Before a turn.** If the state is behind the bank, the turn's notes say
  "Bookkeeper covers through X; run `orchestrator bookkeeper-update`". With `ORCHESTRATOR_BK_AUTO=1`,
  `ORCHESTRATOR_BK_INCREMENTAL=1` and a Bookkeeper that has sent a delta report, a quick delta update runs first,
  inside the turn's deadline (20 seconds are kept for the answer). A full rerun never runs automatically.
- **Safety.** The state file is written atomically (temp file, then replace) under a lock file, and the previous
  good copy is kept as `.bak`. A corrupt state file is recovered from `.bak` (the bad one is kept as
  `.corrupt-<time>`), or refused with a message saying what to do. Only `R-*.json` files inside `data/reports` are
  ingested (no `..`, no symlinks). `bookkeeper_status` only reads.
- **One adapter.** `bk_adapter.py` is the only module that knows the Bookkeeper's report and request shapes.

### Verified reports (bk schema 1.2)

A published 1.2 report has `verifier` (AgentInfo) set and `verification.passed: true`; its `verification.notes`
say which published findings the Verifier replaced or found alone, and which of the Bookkeeper's it left out.
`bk_adapter.verified_status(report)` parses them, keyed on the PUBLISHED ids (`F{n} (...): the Verifier's version
replaces the Bookkeeper's Fx`, `F{n} (...): found by the Verifier; the Bookkeeper did not report it`, `Left out the
Bookkeeper's Fx`; compare.py's verdict notes use draft ids and are ignored). Note `passed` is true on EVERY
published 1.2 report: "verified" means "went through the loop", not "full agreement".

**The owner decides every finding** (see `ORCHESTRATOR.md`, "owner review before forecasting"): accept, dismiss or
revise. An LLM never records that decision, and neither does the Verifier agreeing with a finding.
`checks_bk.check_findings(report, findings, decisions=None)` replaces the direct use of `checks_interim`:

| Owner's decision | Check | What the forecast may use (`contracts.passed_adjustments`) |
|---|---|---|
| none yet (`decisions=None`) | `downgrade` (awaiting the owner) | only the cautious kinds (`unpaid_bill`, `ongoing_overcharge`), never recoverable: today's behaviour |
| accept | `pass` | all of the finding's adjustments, including `exclude_row` / `correct_row`, `recoverable` as reported |
| dismiss (with the owner's note) | `reject` | none |
| revise (the note goes back to the Bookkeeper for a new, verified report) | `reject` | none |

Decisions may be a dict `{finding_id: "accept" | {"decision", "note"}}` or a list of `FindingDecision`-like
objects (`report_id`, `finding_id`, `decision`, `note`). If decisions are supplied but a finding has none (or a
decision names another report, an unknown finding or an unknown verb), it raises `ValueError("review incomplete:
...")`. Nothing in this branch collects decisions yet: every tool, the brief and the API call it with none, so every
finding awaits the owner. The stage gates and the owner-review flow come next, on this hook.

What the Verifier said is **information for the owner**: `bk_state` stores each finding's status (`agreed` /
`replaced` / `verifier_only` / `unverified`) with the report that last listed it (an unverified rerun resets it to
`unverified`), and the check reasons, `get_findings` (`verification`, `verification_label`), the brief,
`bookkeeper-status` and `/api/brief` show "verified by the Bookkeeper's Verifier (N rounds)" or "not verified" and,
per finding, "Verifier agreed", "Verifier's version" or "found by the Verifier".

### What the Bookkeeper owner needs to add

Today every run is a full snapshot (`as_of` = the latest bank date), ids are renumbered, and `ReportRequest`
forbids extra fields. For quick updates the Bookkeeper should accept and report these fields. The names are
suggestions; if they end up different, change only the constants at the top of `bk_adapter.py`:

| Field | Where | Meaning |
|---|---|---|
| `since` | `ReportRequest` and CLI `--since`; echoed in `window.since` | review only bank rows on or after this date |
| `prior_report_id` | `ReportRequest` and CLI `--prior-report-id` | the report this delta continues (for context) |
| `mode` | `Report` | `"full"` or `"delta"` |
| `coverage.window` | `Report.coverage` | `{start, end}`: the days this delta re-examined |
| `finding_key` | each `Finding` | an id that stays the same across runs |
| `status` | each `Finding` | `"open"`, `"resolved"` or `"superseded"` (a delta closes findings with it) |

When that ships, set `ORCHESTRATOR_BK_INCREMENTAL=1` so the orchestrator sends `since` and `prior_report_id`.
Optionally also set `ORCHESTRATOR_BK_AUTO=1` so chats catch up on their own. Until then the flag stays off: bk's
`ReportRequest` forbids extra fields and its argparse rejects unknown flags, so nothing extra is ever sent.

**The Verifier's port.** `bookkeeper serve-verifier` takes `--port` (default 8003), and `bookkeeper serve`
reaches it at `BOOKKEEPER_VERIFIER_ENDPOINT` (default `http://127.0.0.1:8003/submit`). With the port plan above the
defaults are right; nothing to change. The exact commands, from `nessie_lab/`:

```bash
uv run --project bookkeeper bookkeeper serve-verifier --port 8003
uv run --project bookkeeper bookkeeper serve --port 8001
```

**Replying to the orchestrator.** In local mode the Bookkeeper's resolver (`RulesBasedResolver`) knows only its
demo client (8002) and its Verifier (8003), so it can receive the orchestrator's request but cannot route the reply
back. Two ways out:

1. **Mailbox mode** for both sides: `BOOKKEEPER_MAILBOX=true` and `VERIFIER_MAILBOX=true` on the Bookkeeper,
   `ORCHESTRATOR_MAILBOX=1` on the orchestrator (a local-mode orchestrator never registers, so a mailbox Bookkeeper
   could not find it either), each connected to its Agentverse mailbox.
2. **Local peers** (the teammate adds two env vars to `bookkeeper/agent.py`'s `make_agent`): merge
   `BOOKKEEPER_PEERS` (parsed like `ORCHESTRATOR_PEERS`) into the `peers` dict it passes to `local_agent`, and,
   optionally, answer only `BOOKKEEPER_ALLOWED_SENDERS` (today it answers any sender). The lines for the
   Bookkeeper's `.env`:

   ```bash
   BOOKKEEPER_PEERS=agent1<orchestrator address>=http://127.0.0.1:8005/submit
   BOOKKEEPER_ALLOWED_SENDERS=agent1<orchestrator address>,agent1<bookkeeper demo client address>
   ```

   and for the orchestrator's:

   ```bash
   BOOKKEEPER_ADDR=agent1<bookkeeper address>
   ORCHESTRATOR_PEERS=agent1<bookkeeper address>=http://127.0.0.1:8001/submit,agent1<scout address>=http://127.0.0.1:8006/submit
   ```

   The orchestrator's address is printed by `orchestrator serve` (it is fixed by `ORCHESTRATOR_SEED`).

Report ids: the Bookkeeper keys a report on `sha256(sender \0 request_id)`. Through the CLI the sender is `local`;
through the agent it is the orchestrator's address, and the orchestrator's timeout hints use that.

## The uAgent (`agent.py`)

Agent Chat Protocol on uagents 0.26.1, `handle_messages_concurrently=True`. Each message is acknowledged first.
The TextContent parts are joined, and `StartSessionContent` starts a fresh session for that sender. A second
message from a sender whose turn is still running gets `busy`. The reply is TextContent plus `EndSessionContent`.

When the turn proposed a Suggestion and cards are on, the reply also carries an ASI:One Interactive Card
(`MetadataContent`):

- `card_protocol_version: "1"` and `card_kind: "review"`;
- a `card_payload` with string-only `summary_rows`, `Save` and `Discard` CTAs, and every value a string;
- no `EndSessionContent` in that reply, so the chat stays open for the click.

A click comes back either as a card response (`MetadataContent` selection) or as JSON text, and both go through
the same Save/Discard path.

**Local mode** (default):

- **Bind address and registry.** It binds 127.0.0.1 by setting `uagents.asgi.HOST`, because 0.26.1 has no host
  option, and it never registers publicly.
- **Who it answers.** Only `ORCHESTRATOR_ALLOWED_SENDERS`, or else only this machine's local client, whose seed is
  `ORCHESTRATOR_LOCAL_CLIENT_SEED` or a random one kept in `data/orchestrator/local_client_seed` (gitignored).
  Other senders get an acknowledgement and nothing else, so two agents that refuse each other can't loop.
- **Where replies go.** To the local client endpoint (`ORCHESTRATOR_LOCAL_CLIENT_ENDPOINT`, default
  `http://127.0.0.1:8007/submit`) and to `ORCHESTRATOR_PEERS`.

**Mailbox mode** (`serve` with `ORCHESTRATOR_MAILBOX=1`, or `make_agent(..., mailbox=True)`):

- **Who it answers.** Anyone on ASI:One, rate limited to 10 messages per sender per 10 minutes. Messages over
  4,000 characters are refused.
- **Resolver.** It checks `ORCHESTRATOR_PEERS` first, then the global Almanac.
- **No local endpoint.** It passes no `endpoint`: in uagents 0.26.1 an explicit endpoint overrides `mailbox=True`.
- **Who may propose.** Only `ORCHESTRATOR_OWNERS` can make it call `propose_fix`. Everyone else gets facts (the
  brief without fixes, cached per day) and "ask the owner", and no reply carries an absolute path.
- **Bind and inspector.** It refuses to start unless bound to a loopback address. The inspector routes are needed
  only for `POST /connect`: `/messages` is never served, `/connect` needs the REST token, and both `/connect` and
  `/disconnect` close after one successful connect (or from the start with `ORCHESTRATOR_INSPECTOR=0`).

**Agent-first delegation.** In `serve`, when `SCOUT_ADDR` / `BOOKKEEPER_ADDR` are set, delegating to those agents
is the DEFAULT; the in-process tool is the fallback, and a reply that used it is marked degraded.
`ORCHESTRATOR_USE_AGENTS=0` turns delegation off, `=1` forces it on. The CLI and `brief` stay in-process and
offline unless `--agents` is passed (then they go through the running `serve`).

- `scan_market` (chat turns, and the brief's market scan, which is fetched from the scout first) is sent to the
  Market scout as a `MarketRequest` in a ChatMessage, with `ctx.send_and_receive(..., timeout=25)`.
- Bookkeeper setup and update (`orchestrator bookkeeper-setup --agents` / `bookkeeper-update --agents`, which call
  `POST /bookkeeper` on the running agent with the bearer token; and the `ORCHESTRATOR_BK_AUTO` delta pre-turn) send
  `{request_id, nessie_tag, as_of}` to the Bookkeeper. It answers once, after its Verifier loop, from the handler of
  the Verifier's reply, i.e. outside `send_and_receive`'s session, so the orchestrator matches the reply by
  `request_id` (`PeerReplies`, at most 8 waiting). A success is `{request_id, report_id, filename, as_of,
  verified}`; `{status: "error", error, trace_id}` is an error. Only a request that never reached the Bookkeeper
  (`AgentUnreachable`) falls back to the subprocess (degraded). One that times out (it may still be running) or
  replies with an error (it ran) is NOT rerun in-process: a second run would double the ASI calls.
- A chat never starts a Bookkeeper run, unless `ORCHESTRATOR_USE_AGENTS=1` is set explicitly: then, as before, an
  owner's `get_findings` with no baseline yet asks the Bookkeeper for one (and usually times out at 25 s).
- In local mode, both peers must be in `ORCHESTRATOR_PEERS` so their replies can be resolved. The scout must also
  list the orchestrator in `MARKET_SCOUT_ALLOWED_SENDERS` and `MARKET_SCOUT_PEERS`
  (`agent1<orchestrator>=http://127.0.0.1:8005/submit`).
- A peer's reply is only acknowledged, never answered (no ping-pong); one a request is waiting for is handed to it.

## HTTP API for the frontend

A clean JSON API on the orchestrator's own server (port 8005), as uagents REST routes (`on_rest_get` /
`on_rest_post` with `uagents.Model` request and response classes, `src/orchestrator/api.py`). The JSON Schemas of
every model and the endpoint list are in [`docs/api_schema.json`](docs/api_schema.json) (regenerate with
`orchestrator api-schema --out docs/api_schema.json`; a test fails when it is stale).

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/health` | | version, mode (local/mailbox), offline, delegation, agents reachable, Bookkeeper coverage |
| GET | `/api/brief` | | the deterministic brief as JSON (cash, runway, bills, findings, market, fixes) plus `text`. Read-only: it never writes a suggestion |
| POST | `/api/ask` | `{"question": "...", "session_id"?: "api", "reset"?: false}` | `{text, suggestions, approve_commands, owner_action_required, degraded, notes, status}`: one owner turn behind the figure guard |
| GET | `/api/status` | | `bookkeeper_status`, cost_price `verify()` summary (`ok`, `problems`, `notes`, `fingerprint`), `pending_suggestions` |
| GET | `/api/suggestions` | | pending suggestion files: `plan_line` (CH's question line), title, action, target, `old_price`/`new_price`, `effect`, `created`, `approve_command`, `owner_action_required: true` |
| POST | `/api/suggestions/preview` | `{"id": "orch-..."}` | CH's dry-run plan, the question line, `approve_command`. Read-only |
| GET | `/api/market` | | the latest saved market positions (no new scan) |
| POST | `/api/forecast` | `{"scenario": {...contracts.Scenario...}}` | `base`, `scenario`, `vs_base`, `monthly_cash_effect`, `effects`. The scenario is validated |
| GET | `/api/menu` | | the effective menu, each dish's food cost, and the menu's overall food cost % |
| GET | `/api/bills` | | pending bills, earliest first, with the total |

- **Money** is `{"cents": 1750, "usd": "$17.50"}`; dates are ISO (`2026-10-24`). Every response has `ok`; when it is
  false, `error` says why (HTTP 200). A body that doesn't match the request model is a 400.
- **There is no approve or apply route**, and none can be added under a name containing approve, apply, revert or
  unlock (`api.register` asserts it; a test checks every registered route and runs every endpoint under the CH and
  owner-key tripwire). To apply a suggestion the owner runs its `approve_command` at their terminal.
- **Who may call it.** Every `/api` route is registered only on a loopback bind, and GuardedServer requires a
  loopback client and `Host`, the bearer token from `nessie_lab/data/orchestrator/rest_token` on every request
  but the CORS preflight (`serve` writes a fresh one at each start), and bodies of at most 64 KiB.
- **CORS.** Set `ORCHESTRATOR_ALLOWED_ORIGINS` (comma-separated, loopback origins only, e.g.
  `http://localhost:3000,http://127.0.0.1:5173`). For those origins only, a preflight `OPTIONS` gets 204 with
  `Access-Control-Allow-Origin: <that origin>`, `Vary: Origin`, `Access-Control-Allow-Headers: Authorization,
  Content-Type`, `Access-Control-Allow-Methods: GET, POST, OPTIONS`, and every response echoes the origin. `*` is
  never sent (and refused in the variable); any other Origin gets 403. CORS never replaces the token.
- `POST /api/ask` runs as the owner (the token holder), like `/ask`: it may write a pending suggestion, never
  approves one. `GET /api/brief` never proposes; ask for a brief through `/api/ask` to have fixes proposed.

```bash
TOKEN=$(cat nessie_lab/data/orchestrator/rest_token)
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8005/api/health
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8005/api/brief
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"scenario": {"name": "fridge", "one_off_costs": [{"cents": 480000, "label": "walk-in fridge"}]}}' \
     http://127.0.0.1:8005/api/forecast
```

```js
// the frontend, served from an origin listed in ORCHESTRATOR_ALLOWED_ORIGINS
const API = "http://127.0.0.1:8005";
const headers = { Authorization: `Bearer ${import.meta.env.VITE_ORCHESTRATOR_TOKEN}` };
const brief = await (await fetch(`${API}/api/brief`, { headers })).json();
const answer = await (await fetch(`${API}/api/ask`, {
  method: "POST",
  headers: { ...headers, "Content-Type": "application/json" },
  body: JSON.stringify({ question: "What should I change on the menu?" }),
})).json();
// answer.approve_commands: show them; the owner runs them at a terminal. There is no approve endpoint.
```

## Agentverse / ASI:One

`./online.sh` (in `nessie_lab/`) brings the orchestrator, the Bookkeeper and its Verifier online together, each in
mailbox mode, and stops them together on Ctrl-C; `nessie_lab/bookkeeper/README.md` ("ASI orchestrator integration")
describes it. They talk through their Agentverse mailboxes and share `data/`, so they run on one machine. In
`nessie_lab/.env`:

1. `ORCHESTRATOR_SEED` (private and stable: it fixes the address) and `ORCHESTRATOR_API_KEY` (or `ASI1_API_KEY`).
2. `BOOKKEEPER_ADDR`: the Bookkeeper's address, which `./online.sh` prints. With the Bookkeeper's seed in the same
   `.env`, the Bookkeeper derives the orchestrator's address from `ORCHESTRATOR_SEED` and runs its requests.
3. `ORCHESTRATOR_OWNERS` and `BOOKKEEPER_ALLOWED_SENDERS`: your ASI:One address, so you can propose changes and, with
   `ORCHESTRATOR_USE_AGENTS=1`, start Bookkeeper runs from chat.
4. `AGENTVERSE_TOKEN`, the first time only: the script creates the mailbox with `python -m orchestrator.register`.
   That POSTs `{"user_token", "agent_type": "mailbox"}` to `http://127.0.0.1:8005/connect` (the endpoint uagents
   0.26.1 adds when the inspector is on) with the agent's REST token from `data/orchestrator/rest_token`. Neither
   token is printed. The browser Agent Inspector can't do this: the agent refuses requests from web origins. Later
   runs start with `ORCHESTRATOR_INSPECTOR=false`.

Then, with the agents online, `orchestrator bookkeeper-setup --agents` (or `bookkeeper-update --agents`) asks the
Bookkeeper over Agentverse. On Agentverse, give the agent a name and a README that includes the two badges above. It
then shows up in ASI:One chat.

## Agents

| Agent | Name on Agentverse | Address |
|---|---|---|
| Orchestrator (ASI) | `hermes_orchestrator` (`@hermes-orchestrator`) | `agent1qd28hf578hly342hr5zq59uc25ra27ae0grc46wq6m9tnzl5f8kx5zyrp8r` |
| Market scout | `<name>` | `agent1<...>` |
| Bookkeeper | `bookkeeper` | `agent1qdjydrlnh7x8fnh4rlyhmfg8q3ehwrwzfgv4g8u9ztxyjmdqaet52nyyk76` |
| Bookkeeper's Verifier | `bookkeeper-verifier` | `agent1qted2t6kzvgwqed6cwzxjk89h47xyelzqp93l49mmwrtck5ekh8hvrf0gt3` |

## Environment

| Variable | Meaning |
|---|---|
| `ORCHESTRATOR_API_KEY` | ASI:One key (falls back to `ASI1_API_KEY`); read from `nessie_lab/.env` or `BackHouse/.env`. Never logged. |
| `ORCHESTRATOR_SEED` | Required for `serve`; no default. |
| `ORCHESTRATOR_MODEL` | Default `asi1`. |
| `ORCHESTRATOR_PEERS` | `agent1...=http://127.0.0.1:PORT/submit`, comma-separated. These are only reply routes and grant nothing. Local hosts only, unless `ORCHESTRATOR_PEERS_ALLOW_REMOTE=1`. A malformed entry stops `serve`. |
| `ORCHESTRATOR_ALLOWED_SENDERS` | Local mode: who may chat (comma-separated). Unset means only the local client. |
| `ORCHESTRATOR_OWNERS` | Mailbox mode: agent addresses allowed to make it propose changes (comma-separated). Unset means nobody. |
| `ORCHESTRATOR_INSPECTOR` | Mailbox mode: `0` turns the inspector routes (`/connect`, `/disconnect`) off from the start. |
| `ORCHESTRATOR_CARDS` | `1`/`0`: Interactive Cards. On by default in mailbox mode. |
| `ORCHESTRATOR_USE_AGENTS` | Override for delegation in `serve`: unset = on when an address below is set; `0` off; `1` on (and an owner's chat may ask the Bookkeeper for a first run). |
| `SCOUT_ADDR`, `BOOKKEEPER_ADDR` | The Market scout (8006) and Bookkeeper (8001) agent addresses; setting one makes `serve` delegate to it. |
| `ORCHESTRATOR_ALLOWED_ORIGINS` | Browser origins for the `/api` CORS answer, comma-separated, loopback only (e.g. `http://localhost:3000`). Unset = no browser origin is allowed (curl and server-side callers still work). |
| `ORCHESTRATOR_LOCAL_CLIENT_ENDPOINT` | Local mode: the local chat client's endpoint (default `http://127.0.0.1:8007/submit`). |
| `ORCHESTRATOR_BK_INCREMENTAL` | `1`: send `since` / `prior_report_id` to the Bookkeeper (only once it accepts them). |
| `ORCHESTRATOR_BK_AUTO` | `1`: a turn may run a quick delta update first (needs INCREMENTAL and a delta-capable Bookkeeper). Never a full run. |
| `ORCHESTRATOR_BK_UV` | The uv command used to run the Bookkeeper (default `uv` on PATH, else `python -m uv`). |
| `AGENTVERSE_TOKEN` | For `orchestrator.register` only. Never printed. |
| `ORCHESTRATOR_MAILBOX`, `ORCHESTRATOR_BIND_HOST`, `ORCHESTRATOR_OFFLINE`, `ORCHESTRATOR_TAG` | Mailbox mode, the bind address (default 127.0.0.1), offline market scans (default on) and the Nessie tag (default `boh-1loc`). |

## Safety rules

- **No change without the owner.** No code path approves, applies live, unlocks the owner key or writes cost_price.
  `propose_fix` writes a pending Suggestion file, and the owner approves it at a terminal with the passphrase.
  `test_source_never_approves_applies_or_unlocks` and the tripwire tests (which patch `Executor.apply`,
  `Executor.record_approval`, `api.record_approval` and `owner.unlock` to fail) guard this. Every reply ends with
  "Nothing has been changed; approve at your terminal to apply.", and the guard cuts sentences that say a change
  was applied, is live, is in effect, "now costs", or that "I changed/updated/raised/lowered" something.
- **Every number from a tool, never from an echo.** Support comes only from successful results of the
  orchestrator's own tools, and only from structured fields: numbers (`*_cents` as cents), dates, and the
  `*_usd`/`*_display` strings `fmt.py` makes. Free text (memos, payees, titles, questions, dish names), error
  results, peer replies and echoed caller input (`args`, `scenario`) never count, so a model can't launder
  "$48,000" through a tool argument. Error messages never quote what the caller sent. Money written in words that
  can't be parsed is unsupported.
- **Normalise, then guard what is sent.** The answer is NFKC-normalised, stripped of format and control characters
  (zero-width spaces, soft hyphens, word joiners) and given one kind of dash before the guard runs.
- **Bounded work.** At most 8 tool calls run per model reply and 3 `propose_fix` calls per turn. `propose_fix`
  reuses a pending suggestion file for the same (action, dish, new price) less than 24 hours old, so repeated
  briefs write no new files. One deadline covers the turn, the brief fallback included. `tools.call` checks it
  before starting and before every write (suggestions, forecasts, market reports), and the turn cancels it when it
  returns, so a leftover worker thread writes nothing. Suggestion ids carry the second and 8 random hex digits.
- **Untrusted files.** Only `data/reports/R-*.json` and `data/market_reports/M-*.json` with names matching
  `^R-[A-Za-z0-9._-]+\.json$` (or `^M-...`) are read, including names a peer agent sends. A report that can't be
  read is listed under `rejected_reports` and the previous valid one is used; it never breaks `forecast_base` or
  `propose_fix`. The scout agent's `MarketResult` is mapped through its report file into the in-process shape, or
  the in-process scan runs and the reply is marked degraded.
- **No loops.** Our own address is dropped. A message carrying `EndSessionContent`, a review card or one of our
  notices (tagged `backhouse_notice` and starting "Hermes notice:") never starts a turn. Rate-limit, busy and
  refusal notices go out once per sender per window. The rate limiter forgets idle senders and keeps at most 5,000.
- **HTTP.** uagents' server is wrapped (`GuardedServer`): no proxy headers (X-Forwarded-For is ignored), a bearer
  token on `/ask`, `/bookkeeper`, `/connect` and every `/api` route, and `/messages` is never served. CORS headers
  only on `/api`, only for `ORCHESTRATOR_ALLOWED_ORIGINS`, never `*`; other web origins are refused (403), and
  `/bookkeeper` refuses every Origin. Mailbox mode binds loopback only. No route approves or applies.
- **No secrets or paths in output.** Keys and tokens never appear in logs or errors. Messages, errors and cards show
  root-relative paths (`data/suggestions/<id>.json`). Only the approve command for a scratch root keeps `--root`
  with its absolute path, and mailbox replies never carry an absolute path.
- **Tool data is data.** Memos, menu text and bank descriptions are untrusted. The system prompt says so, and the
  guard does not trust the model to follow it.
- **Input and limits.** Incoming text is NFKC-normalised and stripped of escapes and control characters. Senders
  are allowlisted (local) or rate limited (mailbox), and messages are size-capped.
- **No live calls in tests.** Tests fake the LLM with `httpx.MockTransport`, call the HTTP app in-process, and any
  real network call fails the test.
