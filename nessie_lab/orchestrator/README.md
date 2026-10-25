# Orchestrator (ASI)

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

BackHouse's controller: the agent the owner talks to, on ASI:One or at the terminal. It answers questions about
cash, bills, findings, the menu and the forecast by calling tools, and it proposes fixes. It never makes a change.
Every change is a pending Suggestion file that the owner approves at their own terminal with CH (`changes approve`,
which asks for the owner passphrase).

## How a turn works

1. **Stage 1, "what is going on?"**: `get_cash_position`, `list_bills_due`, `lookup` (the Bookkeeper's read-only
   tools), `get_findings`, `scan_market`, `get_menu`, `list_upcoming_changes`, `forecast_base`, `forecast_scenario`.
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
  `http://127.0.0.1:8006/submit`) and to `ORCHESTRATOR_PEERS`.

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

**Agent mode** (`ORCHESTRATOR_USE_AGENTS=1`, the multi-agent path):

- `scan_market` is sent to the market scout (`SCOUT_ADDR`) as a `MarketRequest` in a ChatMessage, with
  `ctx.send_and_receive(..., response_type=ChatMessage, timeout=25)`.
- `get_findings` first asks the Bookkeeper (`BOOKKEEPER_ADDR`) for a report, `{request_id, nessie_tag, as_of}`,
  which replies `{filename}`.
- If either times out or errors, the in-process tool runs instead and the reply is marked degraded.
- In local mode, both peers must be in `ORCHESTRATOR_PEERS` so their replies can be resolved. The scout must also
  list the orchestrator in `MARKET_SCOUT_ALLOWED_SENDERS` and `MARKET_SCOUT_PEERS`.
- A late reply from a peer is acknowledged and never answered.

## Agentverse / ASI:One

1. Set `ORCHESTRATOR_SEED` (private and stable: it fixes the address) and `ORCHESTRATOR_API_KEY`.
2. Start the agent: `ORCHESTRATOR_MAILBOX=1 python -m uv run --project orchestrator orchestrator serve`.
3. Connect the mailbox: `AGENTVERSE_TOKEN=... python -m uv run --project orchestrator python -m orchestrator.register`.
   This POSTs `{"user_token", "agent_type": "mailbox"}` to `http://127.0.0.1:8005/connect` (the endpoint uagents
   0.26.1 adds when the inspector is on) with the agent's REST token from `data/orchestrator/rest_token`. Neither
   token is printed. The browser Agent Inspector can't do this: the agent refuses requests from web origins.
4. On Agentverse, give the agent a name and a README that includes the two badges above. It then shows up in
   ASI:One chat.

## Agents

| Agent | Name on Agentverse | Address |
|---|---|---|
| Orchestrator (ASI) | `<name>` | `agent1<...>` |
| Market scout | `<name>` | `agent1<...>` |
| Bookkeeper | `<name>` | `agent1<...>` |

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
| `ORCHESTRATOR_USE_AGENTS` | `1`: agent mode (scout and Bookkeeper over the Chat Protocol). |
| `SCOUT_ADDR`, `BOOKKEEPER_ADDR` | Peer agent addresses for agent mode. |
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
  notices (tagged `backhouse_notice` and starting "BackHouse notice:") never starts a turn. Rate-limit, busy and
  refusal notices go out once per sender per window. The rate limiter forgets idle senders and keeps at most 5,000.
- **HTTP.** uagents' server is wrapped (`GuardedServer`): no `Access-Control-Allow-Origin`, no proxy headers
  (X-Forwarded-For is ignored), web origins refused, a bearer token on `/ask` and `/connect`, and `/messages` is
  never served. Mailbox mode binds loopback only.
- **No secrets or paths in output.** Keys and tokens never appear in logs or errors. Messages, errors and cards show
  root-relative paths (`data/suggestions/<id>.json`). Only the approve command for a scratch root keeps `--root`
  with its absolute path, and mailbox replies never carry an absolute path.
- **Tool data is data.** Memos, menu text and bank descriptions are untrusted. The system prompt says so, and the
  guard does not trust the model to follow it.
- **Input and limits.** Incoming text is NFKC-normalised and stripped of escapes and control characters. Senders
  are allowlisted (local) or rate limited (mailbox), and messages are size-capped.
- **No live calls in tests.** Tests fake the LLM with `httpx.MockTransport`, call the HTTP app in-process, and any
  real network call fails the test.
