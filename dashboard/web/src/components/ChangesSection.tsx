import { IconCheck, IconMessageCircle, IconPencil, IconX } from "@tabler/icons-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { owner, remember, type Fix, type Plan, type PricePosition, type StepEvent } from "../api";
import { mediumDate, money, moneyExact } from "../format";
import { OwnerDialog } from "./OwnerDialog";

const pct = (share: number) => `${Math.round(Math.abs(share) * 100)}%`;
const price = (cents: number) => (cents % 100 ? moneyExact(cents) : money(cents));

/** A dish and price the owner arrived with (the Chat's "Set Funghi Pizza to $25 on the dashboard" button). */
export type Focus = { dish: string; price: number | null };

/** Why the orchestrator suggests a change, from the numbers it used. */
function reason(fx: Fix, seen?: PricePosition) {
  if (fx.action === "remove_item" && fx.food_cost_pct != null && fx.price_cents != null)
    return `Its ingredients cost ${pct(fx.food_cost_pct)} of its ${price(fx.price_cents)} price.`;
  if (fx.market_median_cents != null && seen)
    return `Nearby restaurants charge ${price(fx.market_median_cents)} in the middle of ${seen.n_prices} menus; you charge ${price(seen.our_price_cents)}.`;
  return null;
}

/** Opens the Chat with this suggestion already typed, for the owner to finish the question ("what if $20 instead?"). */
function askChat(fx: Fix, days: number | null) {
  // The suggestion's own numbers go along, so the Chat compares against them instead of guessing them.
  const effect = [days ? `cash lasts about ${days} days longer on average` : null, fx.monthly_cents ? `about ${money(fx.monthly_cents)} more each month` : null]
    .filter(Boolean)
    .join(", ");
  remember("boh-chat-draft", `About the suggestion "${fx.title}"${effect ? ` (${effect})` : ""}: `);
  window.location.hash = "#/chat";
}

/** The owner's own price for a dish: the orchestrator forecasts it and Changes dry-runs it, and the card shows the result. */
function OwnPrice({ dish, start, auto, onFix, onCancel }: { dish: string; start: number | null; auto: boolean; onFix: (fx: Fix) => void; onCancel?: () => void }) {
  const [value, setValue] = useState(start ? (start / 100).toFixed(2) : "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ran = useRef(false);

  const check = async (cents: number) => {
    setBusy(true);
    setError(null);
    try {
      onFix((await owner.propose(dish, cents)).fix);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  // Arriving from the Chat with a price: check it straight away, so the numbers are ready to approve.
  useEffect(() => {
    if (!auto || !start || ran.current) return;
    ran.current = true;
    void check(start);
  }, [auto, start]); // eslint-disable-line react-hooks/exhaustive-deps

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const cents = Math.round(parseFloat(value.replace(/[$,\s]/g, "")) * 100);
    if (!Number.isFinite(cents) || cents <= 0) return setError("Enter a price, like 25.00.");
    void check(cents);
  };

  return (
    <form onSubmit={submit} className="mt-3 rounded-md border-2 border-rule bg-paper p-3">
      <div className="flex flex-wrap items-end gap-2">
        <label className="text-sm font-bold text-ink">
          Your price for {dish}
          <span className="mt-1 flex items-center rounded-md border-2 border-rule bg-paper focus-within:border-ink">
            <span aria-hidden="true" className="pl-3 text-pencil">
              $
            </span>
            <input
              inputMode="decimal"
              autoFocus={!auto}
              value={value}
              onChange={(e) => setValue(e.target.value)}
              className="min-h-11 w-28 bg-transparent px-2 text-base text-carbon outline-none"
            />
          </span>
        </label>
        <button type="submit" disabled={busy} className="min-h-11 rounded-md bg-ink px-4 text-[0.9375rem] font-bold text-paper hover:bg-ink/90 disabled:opacity-60">
          {busy ? "Checking…" : "Check this price"}
        </button>
        {onCancel && (
          <button type="button" onClick={onCancel} className="min-h-11 rounded-md px-3 text-[0.9375rem] font-bold text-ink underline underline-offset-4 hover:bg-check">
            Cancel
          </button>
        )}
      </div>
      <p className="mt-2 text-sm text-pencil">
        The orchestrator forecasts your price and Changes dry-runs it; then you approve or decline it here. Up to 30% from today's price.
      </p>
      {error && (
        <p role="alert" className="mt-2 text-sm font-bold text-serial">
          {error}
        </p>
      )}
    </form>
  );
}

type Outcome = { kind: "approved"; line: string } | { kind: "declined" };

/** One suggestion as a guest check: what it does to your cash, why, and the owner's answers to it: approve, decline,
 * set their own price, or ask the Chat. */
function FixCard({
  fx,
  n,
  seen,
  ownerKey,
  onOwnerKey,
  focus,
}: {
  fx: Fix;
  n: number;
  seen?: PricePosition;
  ownerKey: boolean;
  onOwnerKey: () => void;
  focus?: Focus;
}) {
  const [current, setCurrent] = useState(fx); // the orchestrator's suggestion, or the owner's own price for the dish
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [approving, setApproving] = useState(false);
  const [editing, setEditing] = useState(!!focus && fx.action === "reprice_item" && focus.price !== fx.new_price_cents);
  const [error, setError] = useState<string | null>(null);
  const card = useRef<HTMLLIElement>(null);
  const days = current.days_later == null ? null : Math.round(current.days_later);
  const why = reason(fx, seen);
  const own = current.id !== fx.id;
  const moved =
    current.runout_before && current.runout_after && current.runout_after !== current.runout_before ? [current.runout_before, current.runout_after] : null;

  // Arriving from the Chat: bring the card into view, and again once the owner's price is checked (the page above it can
  // grow meanwhile).
  useEffect(() => {
    if (focus) card.current?.scrollIntoView({ block: "center", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  }, [focus, current.id]);

  const approve = async (passphrase: string) => {
    if (!ownerKey) {
      await owner.setup(passphrase);
      onOwnerKey(); // the key exists from here on, even if approving fails
    }
    const r = await owner.approve(current.file, passphrase);
    if (r.applied && !r.applied.applied) throw new Error(r.applied.error ?? "Changes signed it but couldn't apply it.");
    setApproving(false);
    setOutcome({ kind: "approved", line: r.line });
  };

  const decline = async () => {
    setError(null);
    try {
      await owner.decline(current);
      setOutcome({ kind: "declined" });
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const button = "inline-flex min-h-11 items-center gap-1.5 rounded-md border-2 px-4 text-[0.9375rem] font-bold transition-colors duration-150";
  return (
    <li
      ref={card}
      className={`stub scroll-mt-40 rounded-md border-2 bg-paper shadow-[0_14px_24px_-20px_rgba(31,77,54,0.5)] ${focus ? "border-ink ring-4 ring-check-deep" : "border-rule"}`}
    >
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2 px-5 pt-4">
        <div className="min-w-0">
          <p className="font-display text-xl font-extrabold leading-none text-serial [font-stretch:66%]">
            No. {n}
            {own && <span className="ml-2 align-middle font-sans text-sm font-bold text-ink [font-stretch:100%]">Your price</span>}
          </p>
          <h3 className="mt-1.5 font-display text-2xl font-extrabold leading-tight text-ink [font-stretch:80%]">{current.title}</h3>
        </div>
        {days != null && days > 0 && (
          <p className="sm:text-right">
            <span className="block font-display text-3xl font-extrabold leading-none text-ink [font-stretch:80%]">+{days} days</span>
            <span className="text-sm text-pencil">of cash, on average</span>
          </p>
        )}
      </div>
      <div className="space-y-1.5 px-5 pb-4 pt-3 text-[0.9375rem] text-carbon">
        <p>
          {days != null && days > 0 ? `Cash lasts about ${days} days longer on average` : "It helps your cash"}
          {current.monthly_cents ? `, and you keep about ${money(current.monthly_cents)} more each month` : ""}.
          {moved && ` Likely out of cash ${mediumDate(moved[1])} instead of ${mediumDate(moved[0])}.`}
        </p>
        {why && <p>{why}</p>}
        {current.starts && <p className="text-pencil">Starts {mediumDate(current.starts)} once you approve it.</p>}
      </div>
      <div className="border-t-2 border-dashed border-rule bg-check/50 px-5 py-4" aria-live="polite">
        {outcome?.kind === "approved" ? (
          <p className="flex items-start gap-2 text-[0.9375rem] font-bold text-ink">
            <IconCheck aria-hidden="true" size={20} className="mt-0.5 shrink-0" />
            Approved and done: {outcome.line}.
          </p>
        ) : outcome?.kind === "declined" ? (
          <p className="flex items-start gap-2 text-[0.9375rem] font-bold text-carbon">
            <IconX aria-hidden="true" size={20} className="mt-0.5 shrink-0" />
            Declined. Nothing changed, and it won't be suggested again.
          </p>
        ) : (
          <>
            <div className="flex flex-wrap gap-2">
              <button type="button" onClick={() => setApproving(true)} className={`${button} border-ink bg-ink text-paper hover:bg-ink/90`}>
                <IconCheck aria-hidden="true" size={18} />
                Approve
              </button>
              <button type="button" onClick={decline} className={`${button} border-ink bg-paper text-ink hover:bg-check`}>
                <IconX aria-hidden="true" size={18} />
                Decline
              </button>
              {current.action === "reprice_item" && current.dish && !editing && (
                <button type="button" onClick={() => setEditing(true)} className={`${button} border-ink bg-paper text-ink hover:bg-check`}>
                  <IconPencil aria-hidden="true" size={18} />
                  Set your own price
                </button>
              )}
              <button type="button" onClick={() => askChat(current, days)} className={`${button} border-transparent text-ink underline underline-offset-4 hover:bg-check`}>
                <IconMessageCircle aria-hidden="true" size={18} />
                Ask the Chat
              </button>
            </div>
            {editing && current.dish && (
              <OwnPrice
                dish={current.dish}
                start={focus?.price ?? current.new_price_cents}
                auto={!!focus?.price && focus.price !== fx.new_price_cents}
                onFix={(next) => {
                  setCurrent(next);
                  setEditing(false);
                }}
                onCancel={() => setEditing(false)}
              />
            )}
            <p className="mt-2 text-sm text-pencil">Approving asks for your owner passphrase, then the orchestrator has Changes update your menu.</p>
            {error && (
              <p role="alert" className="mt-2 text-sm font-bold text-serial">
                {error}
              </p>
            )}
          </>
        )}
      </div>
      <OwnerDialog
        open={approving}
        title="Approve this change"
        what={`${current.title}${current.starts ? `, from ${mediumDate(current.starts)}` : ""}`}
        action="Approve and apply"
        setup={!ownerKey}
        onClose={() => setApproving(false)}
        onSign={approve}
      />
    </li>
  );
}

function where(p: PricePosition) {
  if (p.position === "at_par" || p.gap_pct == null) return "At market";
  return `${pct(p.gap_pct)} ${p.position === "below_market" ? "below" : "above"}`;
}

/** Market scout's comparison: each dish it could price against nearby menus, most below market first. */
function Prices({ market }: { market: NonNullable<Plan["market"]> }) {
  const rows = market.positions.filter((p) => p.position !== "insufficient_data").sort((a, b) => (a.gap_pct ?? 0) - (b.gap_pct ?? 0));
  const thin = market.positions.length - rows.length;
  const cell = "px-3 py-2.5 text-right";
  return (
    <div className="mt-10">
      <h3 className="font-display text-2xl font-extrabold text-ink [font-stretch:80%]">How your prices compare</h3>
      <p className="mt-1 max-w-[62ch] text-[0.9375rem] text-carbon">
        Market scout read nearby restaurants' own menus{market.offline ? ", from saved copies of their pages" : ""}. Delivery apps aren't used, since they mark prices up.
      </p>
      {rows.length ? (
        <div className="mt-3 overflow-x-auto rounded-lg border-2 border-rule">
          <table className="w-full min-w-[36rem] text-[0.9375rem]">
            <caption className="sr-only">Your dinner prices next to nearby restaurants' prices, most below market first</caption>
            <thead className="bg-check text-sm text-pencil">
              <tr>
                <th scope="col" className="px-3 py-2 text-left font-bold">Dish</th>
                <th scope="col" className="px-3 py-2 text-right font-bold">You</th>
                <th scope="col" className="px-3 py-2 text-right font-bold">Nearby, middle</th>
                <th scope="col" className="px-3 py-2 text-right font-bold">Nearby range</th>
                <th scope="col" className="px-3 py-2 text-right font-bold">Menus</th>
                <th scope="col" className="px-3 py-2 text-right font-bold">Where you sit</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((p) => (
                <tr key={p.our_item} className="border-t border-rule">
                  <th scope="row" className="px-3 py-2.5 text-left font-bold text-carbon">
                    {p.our_item}
                  </th>
                  <td className={cell}>{price(p.our_price_cents)}</td>
                  <td className={cell}>{p.competitor_median_cents != null ? price(p.competitor_median_cents) : "n/a"}</td>
                  <td className={`${cell} text-pencil`}>
                    {p.competitor_low_cents != null && p.competitor_high_cents != null
                      ? `${price(p.competitor_low_cents)} to ${price(p.competitor_high_cents)}`
                      : "n/a"}
                  </td>
                  <td className={`${cell} text-pencil`}>{p.n_prices}</td>
                  <td className={`${cell} ${p.position === "below_market" ? "font-bold text-ink" : "text-carbon"}`}>{where(p)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {thin > 0 && (
        <p className="mt-2 text-sm text-pencil">
          {thin} more dish{thin === 1 ? " has" : "es have"} too few nearby prices to compare.
        </p>
      )}
    </div>
  );
}

/** "Changes to try": what the orchestrator suggests at the end of a check, each with what it does to your cash. */
export function ChangesSection({
  plan,
  market,
  step,
  started,
  ownerKey,
  onOwnerKey,
  focus,
}: {
  plan: Plan | null;
  market?: StepEvent;
  step?: StepEvent;
  started: boolean;
  ownerKey: boolean;
  onOwnerKey: () => void;
  focus?: Focus | null;
}) {
  // A dish the owner arrived with that has no open suggestion (approved already, or never suggested): their own price
  // for it becomes a suggestion here, or the orchestrator says why it can't.
  const [extra, setExtra] = useState<Fix | null>(null);
  const fixes = [...(plan?.fixes ?? []), ...(extra ? [extra] : [])];
  const orphan = !!focus && !!plan && plan.stage?.stage === "Suggest" && !fixes.some((fx) => fx.dish === focus.dish);
  const note = plan
    ? fixes.length || orphan
      ? null
      : plan.stage?.stage === "Audit"
        ? `The orchestrator suggests changes once the Bookkeeper has checked your books. ${plan.stage.why ?? ""}`
        : plan.stage?.stage && plan.stage.stage !== "Suggest"
          ? "The orchestrator suggests changes after you review the Bookkeeper's findings below and sign your review."
          : "Nothing the forecast supports right now. The orchestrator only suggests changes that keep your cash longer."
    : !started && !focus
      ? "After a check, the orchestrator suggests changes here, each with what it does to your cash. Nothing changes until you approve it."
      : step?.state === "waiting"
        ? step.text
        : step?.state === "working"
          ? "The orchestrator is weighing changes that help your cash."
          : market?.state === "working"
            ? "Market scout is comparing your prices with nearby restaurants' menus."
            : focus
              ? `Finding the ${focus.dish} change…`
              : "The orchestrator suggests changes once the forecast is ready.";
  const seen = new Map((plan?.market?.positions ?? []).map((p) => [p.our_item, p]));

  return (
    <section aria-labelledby="changes-heading" className="min-w-0">
      <h2 id="changes-heading" className="font-display text-3xl font-extrabold text-ink [font-stretch:80%]">
        Changes to try
      </h2>
      {plan && fixes.length > 0 && (
        <p className="mt-2 max-w-[62ch] text-[1.0625rem] text-carbon">
          The orchestrator forecast each change against your cash and nearby menu prices, and every one passed a dry run. Approve the ones you want, set your own
          price, or decline: nothing changes until you approve.
        </p>
      )}
      {orphan && focus && (
        <div className="mt-4 rounded-lg border-2 border-ink p-5">
          <p className="text-[1.0625rem] font-bold text-ink">There's no open suggestion for {focus.dish} right now.</p>
          <p className="mt-1 text-[0.9375rem] text-carbon">Try your own price: if the orchestrator can't take it (say, an approved change hasn't started yet), it says why.</p>
          <OwnPrice dish={focus.dish} start={focus.price} auto={!!focus.price} onFix={setExtra} />
        </div>
      )}
      {fixes.length > 0 && (
        <ol className="mt-5 space-y-5">
          {fixes.map((fx, i) => (
            <FixCard
              key={fx.id}
              fx={fx}
              n={i + 1}
              seen={fx.dish ? seen.get(fx.dish) : undefined}
              ownerKey={ownerKey}
              onOwnerKey={onOwnerKey}
              focus={focus && fx.dish === focus.dish ? focus : undefined}
            />
          ))}
        </ol>
      )}
      {note && (
        <div className="mt-4 rounded-lg border-2 border-dashed border-rule p-6">
          <p className={`max-w-[56ch] text-[1.0625rem] ${step?.state === "waiting" ? "font-bold text-serial" : "text-carbon"}`}>{note}</p>
        </div>
      )}
      {plan?.market && <Prices market={plan.market} />}
    </section>
  );
}
