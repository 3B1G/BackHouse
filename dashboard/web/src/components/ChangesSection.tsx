import { IconCheck, IconCopy } from "@tabler/icons-react";
import { useState } from "react";
import type { Fix, Plan, PricePosition, StepEvent } from "../api";
import { mediumDate, money, moneyExact } from "../format";

const pct = (share: number) => `${Math.round(Math.abs(share) * 100)}%`;
const price = (cents: number) => (cents % 100 ? moneyExact(cents) : money(cents));

/** Why the orchestrator suggests a change, from the numbers it used. */
function reason(fx: Fix, seen?: PricePosition) {
  if (fx.action === "remove_item" && fx.food_cost_pct != null && fx.price_cents != null)
    return `Its ingredients cost ${pct(fx.food_cost_pct)} of its ${price(fx.price_cents)} price.`;
  if (fx.market_median_cents != null && seen)
    return `Nearby restaurants charge ${price(fx.market_median_cents)} in the middle of ${seen.n_prices} menus; you charge ${price(seen.our_price_cents)}.`;
  return null;
}

/** One suggestion as a guest check: what it does to your cash, why, and the command that approves it. */
function FixCard({ fx, n, seen }: { fx: Fix; n: number; seen?: PricePosition }) {
  const [copied, setCopied] = useState(false);
  const days = fx.days_later == null ? null : Math.round(fx.days_later);
  const why = reason(fx, seen);
  const moved = fx.runout_before && fx.runout_after && fx.runout_after !== fx.runout_before ? [fx.runout_before, fx.runout_after] : null;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(fx.approve);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // No clipboard (an insecure page, or permission refused): the command stays on screen to select.
    }
  };

  return (
    <li className="stub rounded-md border-2 border-rule bg-paper shadow-[0_14px_24px_-20px_rgba(31,77,54,0.5)]">
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2 px-5 pt-4">
        <div className="min-w-0">
          <p className="font-display text-xl font-extrabold leading-none text-serial [font-stretch:66%]">No. {n}</p>
          <h3 className="mt-1.5 font-display text-2xl font-extrabold leading-tight text-ink [font-stretch:80%]">{fx.title}</h3>
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
          {fx.monthly_cents ? `, and you keep about ${money(fx.monthly_cents)} more each month` : ""}.
          {moved && ` Likely out of cash ${mediumDate(moved[1])} instead of ${mediumDate(moved[0])}.`}
        </p>
        {why && <p>{why}</p>}
        {fx.starts && <p className="text-pencil">Starts {mediumDate(fx.starts)} once you approve it.</p>}
      </div>
      <div className="border-t-2 border-dashed border-rule bg-check/50 px-5 py-4">
        <p className="text-sm font-bold text-ink">To approve it, run this at your terminal from nessie_lab/. It asks for the owner passphrase.</p>
        <div className="mt-2 flex items-start gap-2">
          <code className="min-w-0 flex-1 break-all rounded-md bg-ink px-3 py-2 font-mono text-[0.8125rem] leading-relaxed text-paper">{fx.approve}</code>
          <button
            type="button"
            onClick={copy}
            className="inline-flex min-h-11 shrink-0 items-center gap-1.5 rounded-md border-2 border-ink bg-paper px-3 text-sm font-bold text-ink transition-colors duration-150 hover:bg-check"
          >
            {copied ? <IconCheck aria-hidden="true" size={18} /> : <IconCopy aria-hidden="true" size={18} />}
            {copied ? "Copied" : "Copy"}
          </button>
          <span role="status" className="sr-only">
            {copied ? "Copied the approve command" : ""}
          </span>
        </div>
      </div>
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
export function ChangesSection({ plan, market, step, started }: { plan: Plan | null; market?: StepEvent; step?: StepEvent; started: boolean }) {
  const note = plan
    ? plan.fixes.length
      ? null
      : "Nothing the forecast supports right now. The orchestrator only suggests changes that keep your cash longer."
    : !started
      ? "After a check, the orchestrator suggests changes here, each with what it does to your cash. Nothing changes until you approve it."
      : step?.state === "waiting"
        ? step.text
        : step?.state === "working"
          ? "The orchestrator is weighing changes that help your cash."
          : market?.state === "working"
            ? "Market scout is comparing your prices with nearby restaurants' menus."
            : "The orchestrator suggests changes once the forecast is ready.";
  const seen = new Map((plan?.market?.positions ?? []).map((p) => [p.our_item, p]));

  return (
    <section aria-labelledby="changes-heading" className="min-w-0">
      <h2 id="changes-heading" className="font-display text-3xl font-extrabold text-ink [font-stretch:80%]">
        Changes to try
      </h2>
      {plan && plan.fixes.length > 0 && (
        <>
          <p className="mt-2 max-w-[62ch] text-[1.0625rem] text-carbon">
            The orchestrator forecast each change against your cash and nearby menu prices. Every one passed a dry run and is saved for you to approve. Nothing changes until you do.
          </p>
          <ol className="mt-5 space-y-5">
            {plan.fixes.map((fx, i) => (
              <FixCard key={fx.id} fx={fx} n={i + 1} seen={fx.dish ? seen.get(fx.dish) : undefined} />
            ))}
          </ol>
        </>
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
