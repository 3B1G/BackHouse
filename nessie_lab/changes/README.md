# Changes (CH)

The tool that applies changes, and the only code that writes. It takes a `Suggestion` and the owner's `Approval`, both
from the shared `contracts.py`, applies the change, and returns an `ExecutionResult`. It is plain code with no model:
agents propose, the owner approves, and CH carries out the change.

## Setup

From `nessie_lab/changes/`:

```bash
python -m uv sync
python -m uv run --project . pytest -q
```

After every test that builds a database, the tests assert `cost_price.verify.verify(db, require_seal=False)["problems"]
== []` (an autouse fixture). Tests that forge rows on purpose are marked `tampered`. The tests use a fake bank and
never call Nessie.

CH depends on the shared `cost_price` package (`../cost_price`, an editable uv path dependency) and keeps every record
in `data/cost_price.db`: approvals, the change log, price and availability changes, disputes and applied results. It
writes only through `connect(db, "changes")`, `transaction(con)` and `cost_price.api`; prices are read with
`api.effective_price` / `api.menu` on a reader connection. Build the database and set the owner passphrase once:

```bash
python -m uv run --project ../cost_price cost-price build
python -m uv run --project ../cost_price cost-price owner-init
```

It also reads `data/nessie_sync_<tag>.db` (bank rows and pending bills, from `python nessie_sync.py --tag boh-1loc`).
Live Nessie writes use `nessie_client.py` and `NESSIE_API_KEY`.

## Use

```bash
uv run --project changes changes approve s.json                      # passphrase, shows it, y/N on the plan, signs
uv run --project changes changes approve s.json --apply --live       # ... and applies it right away
uv run --project changes changes apply s.json                        # dry run: prints the planned changes
uv run --project changes changes apply s.json --live                 # applies an approved suggestion (no passphrase)
uv run --project changes changes log
uv run --project changes changes pending                             # bill moves with an intent but no log row
uv run --project changes changes menu --on 2026-10-25                # menu with CH's changes applied
uv run --project changes changes approve --revert C-S1-1 --apply --live   # undo needs its own approval
uv run --project changes changes revert C-S1-1 --live
```

**The passphrase is the real gate.** It unlocks the owner's Ed25519 key, and only that key can sign an approval; a
process that can write the database but doesn't know the passphrase can't make one that checks out. The passphrase is
read with `cost_price.tty.read_passphrase`, which also refuses piped or redirected stdin (on Windows it asks
`GetConsoleMode`, so `< NUL` is refused too). That check only raises the bar for scripts; it is not what stops them.
Only `approve` needs the passphrase. A live `apply`/`revert` checks the stored signature with the public key.

**The y/N question comes from the plan, never from the AI's title.** `approve` builds the question line from the
fresh plan (`cli.question`), the numbers and dates CH will write, for example:

- `Approve: Tiramisu (dine-in dinner) $13.00 -> $16.90 from 2026-10-25? [y/N]`
- `Mission Dry Goods: 5 bills paid 10 days later (10-24 -> 11-03, ...)`, `Pay MDG-CHS-2 today (2026-10-24)`
- `Take Lobster Ravioli off the menu from 2026-10-25`, `Dispute T68E9A4CFB`
- `Undo C-S1-1: Tiramisu back to $13.00 from 2026-10-25`

The title is shown only as a quoted `AI's title: "..."` line. `monthly_cash_effect_cents`, `one_time_cash_effect_cents`
and `run_out_likely_*` are shown as "AI/forecast claims, not checked by CH". They are still signed, because the
owner saw them.

`approve` writes a receipt `<base32 of the id>.approval.json` in the current directory (the decision plus the signed
subject). Base32 is reversible (`executor.receipt_id`), has a single letter case, so ids never collide even on
Windows, and has no path characters. A Windows reserved stem (CON, PRN, AUX, NUL, COM0-9, LPT0-9) is refused.
Suggestion ids must match `^[A-Za-z0-9._-]{1,100}$` (no `:`). Change ids must match `^[A-Za-z0-9._:-]{1,93}$`.
`revert:<change_id>` is CH's own namespace for undo approvals, so no suggestion can take one of those ids, and the
applied-result lookup is keyed by (id, action, target).

From Python: `key = cost_price.owner.unlock(con, passphrase)`, then `ex = Executor(root, key)`, `shown =
ex.subject(suggestion)`, and `ex.record_approval(suggestion, approval, shown=shown)`. After that,
`Executor(root).apply(suggestion, live=True)` or `.revert(change_id, live=True)` needs no key, and
`Executor(root).apply(suggestion)` is a dry run. Pass `nessie=` (a fake bank), `today=` and `now=` in tests. An
injected clock is honoured in a live run or an approval only while `changes.executor.ALLOW_INJECTED_CLOCK` is true
(the tests' conftest sets it); otherwise those refuse with "for tests only", so a caller can't make an old approval
look fresh. Dry runs may use any clock. Change ids are `C-<suggestion_id>-1` for menu and dispute changes and
`C-<suggestion_id>-<bill id>` for bill moves. A bill move that CH found at its target without a successful PUT of its
own on record gets `C-<suggestion_id>-<bill id>-observed` (see Nessie safety).

## What each action does

| `Suggestion.action` | Writes | Where |
|---|---|---|
| `reprice_item` | new dine-in dinner price from the next day (`new_price_cents`; at most ±30%) | `cost_price.db: price_change` |
| `remove_item` | dish off the menu from the next day | `cost_price.db: availability_change` (available=0) |
| `stretch_supplier` | every pending bill of that supplier paid `extra_days` days later (1–30); autopays untouched | Nessie `PUT /bills/{id}` |
| `pay_bill` | one pending bill (`target` = doc id like `MDG-CHS-10053`) paid today (the signed plan names that date, so apply it the same day) | Nessie `PUT /bills/{id}` |
| `dispute_charge` | a dispute record for one bank row; Nessie cannot reverse a purchase | `cost_price.db: dispute` |
| `other` | refused | – |

Every change also gets a `change_log` row citing its approval id, and a fully applied suggestion gets an `applied`
row. Every bill move first gets a `nessie_intent` row. Child rows have exactly the shapes cost_price checks:

- price: `field='price_cents'`, `ref=item`, `new_value=str(price)`
- availability: `field='on_menu'`, `new_value='yes'|'no'`
- dispute: `system='other'`, `field='dispute'`, `ref=bank_id`, `new_value=state`
- revert: same `ref` and `field` as the reverted change, `new_value` = its `old_value`, and `reverts` = its id

## Rules it enforces

- **No approval, no change.** `approve` signs the owner's decision with `api.record_approval` over the exact subject
  shown: `Suggestion.model_dump(mode="json")` plus a fresh plan:
  - `reprice_item`, `remove_item`: `planned_change`, exactly `api.planned_change(table, row)` for the row CH will
    store (`{table: "price_change", item, channel, service, effective_from, price_cents}` or
    `{table: "availability_change", item, effective_from, available}`), so cost_price v3 readers honour it.
  - `stretch_supplier`, `pay_bill`: `planned_bills`, each bill id with its from and to dates.
  - an undo: exactly `{"revert": change_id}`, plus `planned_change` when it undoes a menu change.

  A live apply re-plans and calls `api.current_approval`, and it refuses on any reason:
  - no approval
  - a rejection as the latest decision
  - edited content
  - a different set of bills or dates than the ones signed
  - a forged or replayed signature
  - an approval more than 24 h old, or dated more than 5 minutes in the future (`decided_at`)
  - a different `planned_change`. Its `effective_from` is tomorrow in San Francisco at the time of the apply, so an
    approval given on one day and applied after midnight is refused ("the date rolled over"): **the owner approves
    again**, and the new approval signs the new date.

  The approval (with its age) is checked again inside the transaction that writes each intent or change, and before
  each PUT, so a rejection or expiry mid-run
  stops every move that hasn't started. Moves that already happened are still logged (cost_price's intent exemption),
  and the run reports instead of crashing; `sqlite3.Error` is caught too. A revert needs its own approval, under the
  suggestion id `revert:<change_id>`.
- **The title can't name a different price.** For a `reprice_item`, every dollar figure in the title
  (`\$\d+(\.\d{2})?`) must be the planned old or new price, or the suggestion is refused at approve (so
  `record_approval` refuses it) and at apply. "Raise Tiramisu from $13 to $14" with `new_price_cents=1690` is
  refused.
- **Fields must belong to the action.** `new_price_cents` is allowed only on `reprice_item` and `extra_days` only on
  `stretch_supplier`. Anything else is refused at approve and at apply. CH enforces this because `contracts.py` is
  shared; `Suggestion`'s validator could enforce it too.
- **Never backdated.** A menu change starts tomorrow in San Francisco time (America/Los_Angeles), and that date is
  signed. cost_price also ignores a change whose `effective_from` is before the approval's `decided_at` date. A later reprice in the books wins over a change, because the books are the
  restaurant's own record.
- **Only the dine-in dinner price changes.** That is the price the Forecaster's `Scenario.price_changes_cents` uses.
- **Dry run by default.** Without `--live`, nothing is written anywhere: no database row, no audit line, and no new
  file in `data/`. A read-only SQLite open of a WAL database would create `-wal`/`-shm` (or write read locks into an
  existing `-shm`), so dry runs, `log` and `menu` read a copy of `cost_price.db` (main file + `-wal`) in a temp
  directory, still through `cost_price.db.connect`. A dry run needs no key and doesn't check the approval; it only
  plans.
- **`books.db` is never written,** because `generate.py` recreates it.
- **Every history table is append-only and hash-chained** (cost_price triggers; `cost-price verify` checks the chains).
  Undo is a new change that reverses the old one. A price revert is refused if a later change has moved the price
  since, because reverting would silently undo that change too. Reverting a removal (and reverting that revert) adds
  another `availability_change` row with available=1 or 0.
- **Applying twice changes nothing:** the second call returns the first result (`api.applied_result`).
- **Nessie safety:** before each PUT, CH re-reads the bill and requires `status == 'pending'`. Then, in its own
  committed transaction, it re-checks the approval and writes a `nessie_intent` with the absolute from and to dates.
  After the PUT it reads the bill again. If the re-read shows the new date, the move is logged, even when the PUT
  itself raised (a lost response). On a retry, a bill that has an intent is handled by its live date:
  - at `intent.to`: logged as done
  - at `intent.from`: PUT to `intent.to`, never a date recomputed from the resynced sync DB
  - anything else: refused as stale

  The same holds for a bill with no intent that isn't at its planned from date. So no bill is ever moved twice, and
  network errors never lose a move that finished. Further rules:
  - **Intents and approvals.** A bill keeps its intent's target only if the intent was written under the current
    approval, or its move is known to have landed (logged, or the sync DB shows the target). Otherwise it is planned
    from scratch. `nessie_intent` is UNIQUE(suggestion_id, bill_id), so an unconfirmed intent of a superseded
    approval that disagrees with the fresh plan blocks that bill: CH refuses and asks for `changes pending`, a resync
    and a **new suggestion id**. If it agrees with the fresh plan, it is reused. A kept intent that contradicts
    `extra_days` (not +N days, or a pay_bill that isn't earlier) is refused, and no bill is ever PUT to a date before
    today. (A cost_price change to UNIQUE(approval_id, bill_id) would allow re-planning under the same id; CH doesn't
    need it.)
  - **Landed moves are always logged.** Before any approval check, a live run re-reads every intent of the
    suggestion that has no `change_log` row. A bill at `intent.to` is logged under the intent's own approval (the
    trigger's intent exemption), even after a rejection or expiry. `changes pending` lists intents with no log row.
  - **Who moved it.** After each PUT that returns without an error, CH writes a `nessie_put` audit line. A bill found
    at its target with no such line (CH's PUT errored, or never ran; maybe someone else moved it) is logged with
    change_id suffix `-observed`: "observed at target, not confirmed as performed by CH". The other columns are as
    usual (`old_value` = from date).
  - **Bank answers.** Every `payment_date` (Nessie and the sync DB) is normalised with
    `date.fromisoformat(str(x)[:10])`, and an unreadable one is refused. A GET that doesn't return an object is a
    reported error. `_execute` reports and audits any `Exception` (never `KeyboardInterrupt`).
  - **One live run per suggestion.** A live apply or revert takes `data/changes-locks/<base32 id>.lock`
    (O_CREAT|O_EXCL), removed at the end. It records the holder's pid. A second concurrent run refuses with "already
    running" while that pid is alive. A lock whose pid is gone (a run killed with `taskkill /F` or SIGKILL) or that
    can't be parsed is cleared and the run proceeds. An empty lock younger than 5 s belongs to a holder that hasn't
    written its pid yet, so it still blocks. Liveness uses `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` and
    `GetExitCodeProcess == STILL_ACTIVE` on Windows (via ctypes), and `os.kill(pid, 0)` elsewhere. A lock older than
    10 minutes is cleared in any case, which covers pid reuse. A dead lock is renamed away before it is deleted, so
    two retries can't delete each other's new lock.
- **Missing inputs are refusals,** not crashes: no `cost_price.db`, no Nessie sync DB, no such bank row.
- **Never:** balances, purchase status, or fake deposits. Nessie can't change the first two, and the Bookkeeper
  reconciles against that bank.
- **Audit:** every approval, live change and live refusal is appended to `data/audit/changes.jsonl` in the shared
  `AuditEntry` shape, for `audit.py`.

After a live Nessie change, run `python nessie_sync.py --tag <tag>` so the Forecaster and Bookkeeper see the new
dates. Test live writes on a throwaway tag, never on the demo tag.
