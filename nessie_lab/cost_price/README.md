# Cost/price database

`data/cost_price.db` is the "Cost / Price" store on the architecture board. It holds what our dishes cost us, what
we charge, what competitors charge (and the evidence for each competitor price), and every price change the owner has
approved. The Market scout (Mk) reads it and adds competitor observations. CH writes our price changes, and only
under a signed owner approval. ASI and the Forecaster read it.

One dependency, `cryptography`, for the owner's Ed25519 signing key and the AES-GCM sealing of it; everything else is
the standard library.

## Setup

From `nessie_lab/`, after `generate.py` and `build_book.py`:

```bash
uv sync --project cost_price
uv run --project cost_price cost-price build        # create or refresh data/cost_price.db
uv run --project cost_price cost-price owner-init   # once: creates the owner signing key; write down the fingerprint
uv run --project cost_price cost-price verify       # schema, chains, signatures, change coverage, seal
uv run --project cost_price cost-price seal         # owner signs the current state (re-run after reviewing new rows)
uv run --project cost_price pytest cost_price/tests -q
```

Other commands: `menu --on YYYY-MM-DD [--service lunch]`, `market`, `competitors`, and `heads`.

The `.db` file is not in git. `build` recreates it from `books.db` and `seed/competitors.json`, and the same inputs give
byte-identical content (tested with a dump hash).

## What's in it

| Layer | Table | Contents | Written by |
|---|---|---|---|
| reference (rebuilt by `build`) | `restaurant` | our location (for distances) | build |
| | `menu_price_base` | every menu price from the books: dine-in lunch and dinner, delivery, with dates | build |
| | `ingredient`, `ingredient_cost_month` | each ingredient and its qty-weighted invoice cost per month, in millicents | build |
| | `recipe_line` | qty of each ingredient per dish, in thousandths | build |
| | `plate_cost_month` | food cost per plate per month, matching `features.py` to the cent | build |
| history (append-only) | `competitor` | real restaurants, keyed by website domain | build (seed), scout (Apify) |
| | `competitor_observation` | one price seen on one menu: quote, URL, reader (`seed`/`rule`/`asi`), confidence | build (seed), scout |
| | `observation_check` | each time a price was checked against a saved page: verified or rejected, plus the snapshot SHA-256 | scout |
| | `market_summary` | per scan and dish: our price, competitor median, gap, n, position | scout |
| | `approval` | the owner's decisions, each HMAC-signed with the owner key | CH (`approve`) |
| | `change_log`, `price_change`, `availability_change`, `dispute`, `applied` | what CH changed, and when | CH |
| | `owner` | the passphrase salt and verifier (one row, set once) | `owner-init` |

Views: `menu_price_timeline` (the books' prices plus approved changes), `observation_status` (each observation with
its latest check), and `plate_cost_current`. Use `api.effective_price(con, item, on)` and `api.menu(con, on)` for the
price in force on a date. The rule: the latest `effective_from` on or before that date wins. On the same date, an
approved change beats the books.

Conventions: integer cents; `YYYY-MM-DD` dates; `YYYY-MM-DDTHH:MM:SSZ` timestamps (UTC). Dishes are referred to by
name, so rebuilding the reference layer can't orphan history.

## Guards

Every guard below has a test in `tests/test_cost_price.py`, and every red-team finding has a regression test.

1. **Roles, enforced inside SQLite.** Open the database only with `cost_price.db.connect(path, role)`:

   | Role | May write |
   |---|---|
   | `reader` | nothing (the connection is also opened read-only) |
   | `scout` | competitor, observation, check, market summary, never a `seed` row |
   | `changes` | approval, Nessie intent, change log, price/availability changes, disputes, applied |
   | `owner` | the owner row, once |
   | `build` | the reference layer, plus seed competitors and observations only |

   An authorizer refuses everything else. That includes UPDATE or DELETE of history, DROP, ALTER, CREATE (also TEMP),
   ATTACH, VACUUM INTO, and any PRAGMA that sets something. Triggers call `cp_role()` so one role can't write
   another's rows. A plain sqlite3 connection has no `cp_role()` and can't insert into those tables at all.
2. **Append-only.** Triggers refuse UPDATE and DELETE on every history table. `recursive_triggers` is on, so the
   delete hidden inside `INSERT OR REPLACE` is refused too. A chain trigger also refuses any insert that isn't
   exactly the next id with the previous row's hash.
3. **The schema is checked whole.** Before any connection is handed out, every table, trigger and view is compared
   with the expected definition statement by statement. A trigger whose body was swapped for a no-op under the same
   name is caught, and writers refuse to start.
4. **Owner signatures.** `owner-init` creates an Ed25519 key. Only the public key, a salt and the private key sealed
   with AES-GCM under a key derived from the passphrase (scrypt, 2^15) are stored. Every approval is signed over its
   decision, the full suggestion text it approves, and its own id and prev_hash, so a re-appended copy of an old
   approval doesn't verify. Checking a signature needs only the public key. Typing the passphrase is required only
   to sign: `approve`, `seal`.
5. **Readers only honour valid changes.** A price or availability change counts only if all of these hold:
   - its approval is signed by the owner key and says "approved";
   - it is the only menu change citing that approval (one approval, one change, also enforced by a unique index);
   - the signed suggestion carries a `planned_change` that equals the stored row field for field (item, channel,
     service, effective_from and price, or availability). See `api.planned_change`;
   - its change-log row matches that row;
   - it doesn't take effect before the approval date;
   - the suggestion's own target and value agree (or it is a revert that restores exactly what the reverted change
     replaced).

   Price rows hung off a bill move, a second change replaying an approval, another channel than the one signed:
   all of these are ignored by `api.effective_price`, `api.menu` and `api.available`, and reported by `verify`.
   Triggers additionally refuse most of them at insert time. A bank move begun under an approval the owner
   revoked mid-run is the one exception the triggers allow: it is recorded beforehand in `nessie_intent`, must
   still be logged, and can never affect a price.
6. **Hash chains and the seal.** `verify` recomputes every chain and reports edited, deleted, inserted or reordered
   rows. It also checks every schema object, including any named `sqlite_*`. `seal` (owner passphrase) signs the
   owner public key, the head of every chain, and digests of the reference layer and of the seed rows into
   `data/cost_price.seal.json`. After that, `verify` also catches:
   - a chain cut off at the end;
   - a rewrite of every hash;
   - an edit to the books' prices;
   - planted seed rows;
   - a database swapped for one with a different owner.

   Once an owner exists, a missing seal is a problem. Rows added after the seal are covered by the chain only: review
   them and re-seal, and re-seal after a legitimate `build` too. Rolling back the database *and* its seal together
   can't be detected from inside the computer: `verify` prints the seal time and the row counts, so compare them with
   your own record.
7. **Constraints.** STRICT tables; positive cents; real calendar dates; http(s) URLs only. Text is bounded and can't
   contain NUL (which would hide what follows); no lone surrogates; no NaN or infinity; no booleans as numbers. Values
   are normalised before hashing, so what is hashed is exactly what is stored. Foreign keys are on for every
   connection.
8. **Competitors are keyed by registrable domain** (`menu.x.com` = `x.com`). The exception is shared menu platforms,
   keyed by host plus first path segment (`toasttab.com/a` is not `toasttab.com/b`). IDNs are stored as punycode.
9. **Passphrase input** comes only from a real console (`tty.read_passphrase`). On Windows it uses GetConsoleMode,
   because `< NUL` fools `isatty()`. This raises the bar for scripts; the passphrase itself is the real gate.
10. **Concurrency.** WAL mode, a 5 s busy timeout, and `BEGIN IMMEDIATE` for every write. Three processes writing at
    once all succeed (tested).

**What the guards can't stop.** Someone with the file and their own tools can still edit it. `verify` then reports
the change. If they also rewrite every hash, or swap in a whole database, the seal catches it, provided the
fingerprint `verify` prints matches the one you wrote down at `owner-init`.
