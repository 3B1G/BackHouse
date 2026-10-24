# nessie_lab: simulated restaurant data + Nessie pipeline

Everything here is **SIMULATED**. "Demo Bistro" is a fictional Italian trattoria on Chestnut St (Marina), SF.
Suppliers, landlord, lender and delivery platforms are fictional.

**One location for now.** `config.ACTIVE_LOCATIONS = ["CHS"]`. Setting it to `["CHS", "UNI"]` rebuilds the original
two-location group (adds Union St / Cow Hollow, inter-account sweeps, group-sized owner draw and loan); that mode reproduces
the earlier v2 data exactly. All numbers below are for the single location.
Generated files (`data/*.db`, `data/*.csv`) and the answer key (`data/grader/`) are not in git: run the scripts below to rebuild them. Every run produces identical files.

## Why synthetic

There is no public dataset with a restaurant's full books (POS sales, supplier invoices, payroll, rent, bank activity).
Maven's "Pizza Place Sales" is fictitious and covers sales only. So we generate one, matched to published figures:

| Metric (full months) | Ours | NRA 2024 full-service median |
|---|---|---|
| Food & beverage cost (COGS, purchases ± inventory) | ~32% | 32.0% |
| Payroll + benefits, tips excluded | ~42.5% | 36.5% |
| Occupancy (rent incl. NNN) | 6.7% | 6.0% |
| Net cash margin | −4.7% | +2.8% pretax |

SF minimum wage $19.18 → $19.61 on 2026-07-01; 15% delivery commission cap; 8.625% sales tax.
The restaurant is deliberately weaker than the median: it's slowly losing money.

## Setup

`pip install -r requirements.txt`. The Nessie key is read from the `NESSIE_API_KEY` environment variable, else from `../BackHouse/.env`, `../.env` or `./.env`.

## Run order

```
python generate.py        # data/books.db, data/bank_feed.csv, data/grader/*
python features.py        # data/features.db
python eda.py             # eda/report.md + charts
python scenario_check.py  # does the data support the demo story?
python baseline_reconciler.py && python grade.py data/flags_baseline.json   # rule-based Bookkeeper baseline + score
python nessie_load.py dry-run                      # validate every payload, no network
python nessie_load.py full --tag <tag>             # ~1,326 calls, ~2 min at 8 workers
python nessie_sync.py --tag <tag>                  # pull back + verify round trip
```

The generator is deterministic (seed 13). Two runs produce byte-identical files.

## What lives where

| File | Who may read it | Contents |
|---|---|---|
| `data/books.db` | agents | The restaurant's own documents (below). No "expected bank" table: agents derive expectations |
| `data/bank_feed.csv` | agents, Nessie loader | Bank-visible columns only: `bank_id, account, date, nessie_type, direction, amount, counterparty (purchases only), memo` |
| `data/features.db` | agents | Feature tables built from the two above |
| `data/grader/` | **scoring scripts only** | `answer_key.json`, `ledger_expected.csv`, `bank_truth.csv`. Never load into Nessie or an agent's context |

### books.db tables

POS: `tickets` (gross, discount, subtotal, tax, tip, tender: card / cash / gift_card / platform), `ticket_lines`, `refunds`,
`gift_cards`, `pos_card_batches` (gross, count, refunds per day; **no fees**: the agent applies the agreement),
`cash_deposit_slips` (POS cash vs counted cash).
Purchasing: `invoices`, `invoice_lines` (real pack sizes), `credit_memos`, `ap_payment_plans`, `inventory_counts` (month-end), `open_ap`.
Labor: `daily_ops` (covers, scheduled vs actual hours, rain, events), `payroll_runs` (wages, salaries, tips paid, employer taxes).
Other: `vendor_bills`, `repair_invoices`, `misc_payables`, `leases` (with escalator), `agreements` (processor 2.6% + 15¢, delivery 15%),
`loans`, `loan_schedule`, `owner_draws`, `internal_transfers`, `processor_disputes`, `sales_tax_returns`, `deposits_in_transit`.
Reference: `locations` (cuisine, neighborhood, zip for the Market scout), `accounts` (opening balances as of 2025-10-25), `menu_prices`
(dine-in lunch/dinner + delivery, with the 2026-04-01 reprice), `recipes`, `ingredients`, `suppliers`.

### features.db tables

| Table | Used by | Notes |
|---|---|---|
| `bank_categorized` | Bookkeeper | Every bank row categorized from memo + payee + the books' vendor lists |
| `feat_daily_sales` | Forecaster | Per location-day; lags and rolling means use only past days; `split` = train/test |
| `feat_cash_daily` | Forecaster | Daily flows by category; balance = opening + activity (Nessie never updates balances) |
| `feat_item_month` | Controller, Market scout | Realized price, plate cost at that month's invoice prices, fees, profit per plate, Kasavana-Smith class, empty `competitor_median_price` |
| `feat_ingredient_month` | Controller | Weighted invoice price and index per ingredient |
| `feat_supplier` | Controller | Spend, terms, open AP, days a payment could be stretched |
| `feat_recurring` | Forecaster | Recurring payees: amount, variability, usual day |

## Answer key (grader only)

7 errors (one item per root cause) and 6 decoys. Materiality: variances ≤ $10 are noise.
Errors: duplicate supplier debit, supplier debit $100 above invoice, 20% vs 15% delivery commission (3 weeks),
card fee 2.9% vs 2.6% from 2026-08-01, cash deposit $200 short of the slip, undocumented $349 charge, an invoice never paid.
Decoys: supplier credit-memo refund, split payment with a plan, identical standing-order amounts a week apart,
documented chargebacks, card batches settling a day late, a large documented oven repair.
Natural noise that should not be flagged: ±$5 cash over/short at the till, ±$1–9 bank recounts on ~8% of cash deposits, cents-level card fee rounding.

**Baseline** (`baseline_reconciler.py`, books + bank only): recall 7/7, 0 decoy hits, 0 false alarms. Naive strategies (exact-amount matching, same-amount-within-7-days duplicates) fail on the decoys and noise. Score any agent's flags with `python grade.py <flags.json>`; a flag is `{"bank_id": ...}` or `{"books_ref": ...}`.

## Stories already in the data

- **Lobster Ravioli** ran a ~51% food cost (high for a signature dish) and earned ~$12 a plate until a lobster-meat shortage doubled the price on 2026-03-01. Since then food cost is ~95% and it earns about $0 a plate after card and delivery fees, so it loses money once ~$3 of hand-made pasta prep labor is counted. ~190–230 plates a month.
- **Margherita Pizza** is popular with low margin ("plowhorse"): a price-increase candidate. Competitor prices come from the Market scout.
- **Three-payday months** (May 2026; Apr and Jul 2027 ahead) create cash dips. Forecast payroll from its biweekly calendar, never by replaying calendar months.
- `scenario_check.py` (deterministic, daily; payroll, sales tax and rent on their own calendars). Cash $157,631 on 2026-10-24. Reaches $0 on **2027-08-06** (base),
  **2027-07-09** with a $4,800 fridge repair plus a January with 15% fewer covers, and back to **2027-08-06** with fixes (reprice two dishes, fix the ravioli,
  recover the card fee). Below "one payroll + rent" ($50k) from Mar–Apr 2027. Note the bank window holds only 11 sales-tax payments (both October payments
  fall just outside it), so replaying history naively overstates runway.
  The Forecaster's Monte Carlo should replace this.
- Backtest caveat: August and September never appear in the training window, so their seasonality can't be learned. On the two-location data, seasonal regressions scored ~11–14% daily MAPE on the test months and same-day-last-week ~15–18% (not yet re-measured for one location).
  Use `feat_supplier_train` / `feat_recurring_train` (built only from data ≤ 2026-07-24) when backtesting. In `feat_daily_sales`, covers, tickets, hours,
  avg_check and discounts are same-day outcomes: use them as targets, not as predictors for that day.

## Nessie rules learned the hard way

| Behavior | Consequence |
|---|---|
| Account `balance` never changes after creation | Store the opening balance; derive running balances from transactions |
| Purchase `status` can't be updated | Post everything `completed` on the date money moved |
| Transfers have no payee field | Model internal moves as a withdrawal + a deposit |
| Bills: POST accepts a bill without `payment_date` / `recurring_date`, but then **every** `GET /accounts/{id}/bills` for that account returns 400 | Always send both; Nessie computes `upcoming_payment_date` and rejects it on POST |
| `PUT /bills/{id}` with `payment_date` works; `upcoming_payment_date` doesn't move, and on one-off bills shows a misleading next-month date | Agents read and write `payment_date` only. Pending-bill nicknames carry the due date (`MDG-CHS-10053 due 2026-10-24`) |
| `/enterprise/*` endpoints show every team's data, even with a fake key | Nothing real goes in Nessie; everything is tagged `[SIM]` |
| ~0.85 s per call; 8 parallel workers were fine | Full load ≈ 4 min |
| GET lists come back unsorted; `payment_amount` comes back as a float; Nessie assigns its own account numbers | Sort by date yourself; cast amounts to int; memos name accounts by nickname, not number |
| Customers and merchants can't be deleted | POSTs are never auto-retried. The loader writes its manifest atomically after every create, and on each run re-adopts what Nessie already holds (customer by tag, merchants by name, transactions by `|ref=`, bills by nickname, loan by description), so a re-run never double-posts (verified: a second run creates 0). `nessie_sync.py --repair` deletes duplicate refs. To reset, use a new tag |
| No Nessie bill exists for payroll or sales tax | The Forecaster infers both from withdrawals (`BAYPAY PAYROLL`, `CDTFA`) and `sales_tax_returns` |

Bank ids are derived from row content (deterministic). Nessie tag `sample1` predates that and is stale; `sample2` is current.
Tag `boh-dev` (and its manifest) holds the **two-location** data. Load the single-location data under a new tag, e.g. `boh-1loc`.

Amounts are **integer cents everywhere**, including bills, loan and account balances (the account nickname says so).
Each transaction description ends with `|ref=<bank_id>` for round-trip matching only. Agents must not use it to detect errors.
