# BackHouse

**An AI part-time controller for a small restaurant.** It reads the restaurant's bank and paperwork, catches
bookkeeping errors, forecasts when cash runs out, compares menu prices with real nearby restaurants, and proposes
fixes. Nothing changes until the owner approves and signs it.

> *We learn how your restaurant's money normally moves, tell you when it runs out, and show you what to change so
> it doesn't.*

Built at Cal Hacks 13.0 (Oct 23–25, 2026).

## What's real and what's simulated

| Real | Simulated |
|---|---|
| Capital One's **Nessie** sandbox API holds the bank: accounts, 1,263 transactions, 39 bills. CH really moves bill dates through it | The restaurant, "Demo Bistro" on Chestnut St (Marina, SF), and its books. A deterministic generator (seed 13) calibrated to National Restaurant Association medians, SF minimum wage, delivery fee caps and SF sales tax |
| **9 real Italian restaurants** within about 2 miles, and **57 menu prices** quoted from their own websites (51 re-verified against saved copies of the live pages) | 7 planted bookkeeping errors and 6 decoys, so the Bookkeeper can be scored |
| The Monte Carlo forecast, the price comparison, the signatures and the tamper checks all run on that data | Lobster prices doubling in March, and a fridge breaking (the demo scenario) |

How we know it works:
- The rule-based Bookkeeper baseline finds **7 of 7** planted errors with 0 decoy hits and 0 false alarms (`grade.py`).
- The Forecaster has a backtest (`python forecaster.py --backtest`): it forecasts from 2026-07-24 and scores the result
  against what really happened.
- The three shared packages have **449 tests**, including regression tests for three red-team rounds.

## How the pieces fit

```
 Nessie (bank) ──sync──┐                               ┌── Market scout (Mk): our prices vs real competitors
 books.db (the Book) ──┼─> Bookkeeper ─> verifier ─┐   │
                       │                           ├─> ASI orchestrator / Controller ──Suggestion──> owner
                       └─> Forecaster (Monte Carlo)┘   │                                              │ signs
                                                       └── cost/price DB <── approved changes ── CH (changes tool)
                                                                                                      └─> Nessie bills
```

- **Stage 1, "what is going on?":** the Bookkeeper and its verifier, the Forecaster, and the Market scout.
- **Stage 2, "what do we do about it?":** the ASI orchestrator proposes, the owner approves, and CH applies.

| Piece | Where |
|---|---|
| Simulated restaurant, Nessie load and sync, baseline reconciler, grader | `nessie_lab/` ([README](nessie_lab/README.md)) |
| The Book and the Bookkeeper tools; `contracts.py` (the shapes every piece hands to the next) | `nessie_lab/build_book.py`, `bookkeeper_tools.py`, `contracts.py` |
| Forecaster: Monte Carlo cash runway, scenarios, backtest | `nessie_lab/forecaster.py` |
| Bookkeeper agent, verifier, ASI orchestrator and Controller | branch `bk` |
| **Cost/price database**: menu prices, plate costs, competitor prices, signed approvals | `nessie_lab/cost_price/` ([README](nessie_lab/cost_price/README.md)) |
| **Market scout** (uAgent): below, at or above market per dish | `nessie_lab/market_scout/` ([README](nessie_lab/market_scout/README.md)) |
| **CH**: applies owner-signed changes to prices and Nessie bills | `nessie_lab/changes/` ([README](nessie_lab/changes/README.md)) |

## Run it

From `nessie_lab/`, with Python 3.13. The Nessie key goes in `BackHouse/.env` as `NESSIE_API_KEY=...`; it is never
committed.

```bash
pip install -r requirements.txt
python generate.py && python build_book.py && python features.py      # the simulated restaurant (offline)
python nessie_sync.py --tag boh-1loc                                  # pull the bank back from Nessie
python baseline_reconciler.py && python grade.py data/flags_baseline.json
python forecaster.py                                                  # base cash runway
python forecaster.py --scenario '{"name": "fridge", "one_off_costs": [{"cents": 480000, "day": "2026-11-15", "label": "fridge"}], "cover_change": {"2027-01": -0.15}}'
```

The three packages each have their own environment (managed with [uv](https://docs.astral.sh/uv/)):

```bash
python -m pip install uv
python -m uv sync --project cost_price && python -m uv sync --project market_scout && python -m uv sync --project changes
python -m uv run --project cost_price cost-price build        # the cost/price database (from books.db + seed list)
python -m uv run --project cost_price cost-price owner-init   # once: the owner's signing key (passphrase at the terminal)
python -m uv run --project cost_price cost-price seal
python -m uv run --project market_scout market-scout run --request-id demo-1 --items "Margherita Pizza"
python -m uv run --project changes changes approve s.json     # the owner approves a Suggestion; --apply --live applies it
python -m uv run --project cost_price cost-price verify       # signatures, hash chains, seal
```

Run each package's tests with its own path, e.g.
`python -m uv run --project cost_price pytest -q cost_price/tests`.

## What it found in the simulated restaurant

- Cash on 2026-10-24: **$157,631**.
- The likely run-out date is **2027-06-25**. A broken fridge plus a slow January moves it to **2027-05-01, 55 days
  earlier**.
- The card processor has charged **2.9% instead of the agreed 2.6%** since August, one of the 7 errors the
  Bookkeeper catches.
- Our **Margherita is $18**; the median of 7 nearby restaurants is **$21**. Raising it to $20 (5% fewer orders
  assumed) leaves the likely run-out date unchanged and moves the bad case 2 days and the good case 14 days later.
  On its own it isn't enough, which is why the Controller weighs several fixes together.
- **Lobster Ravioli** runs a 95% food cost and earns about $0 a plate.

## Safety by design

- **Agents propose, the owner decides.** CH applies only changes the owner signed with their own key. The approval
  question is built from the exact plan (dish, price, date), never from the AI's wording.
- **The database ignores anything unsigned.** Every change is checked against the signed plan. History is
  append-only and hash-chained, and `cost-price verify` detects edits, deletions and a swapped database.
- **Competitor prices need evidence.** Each price must appear on a saved copy of the restaurant's own menu page, and
  only trusted sources count toward a median.
- **No secrets in git.** API keys stay in `.env`, the database stays on the machine, and the owner's passphrase is
  never stored.
