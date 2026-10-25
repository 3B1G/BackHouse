import type { Forecast } from "../api";
import { fullDate, money, monthDay } from "../format";
import { PaydayStrip, markers } from "./PaydayStrip";

/** The two plain sentences the whole dashboard leads with. */
function sentence(f: Forecast): [string, string] {
  const { safeIndex, flipIndex } = markers(f.paydays);
  const first =
    safeIndex < 0 ? "Payroll is already at risk." : `Payroll is safe through ${monthDay(f.paydays[safeIndex].date)}.`;
  const second =
    flipIndex >= 0 ? `By ${monthDay(f.paydays[flipIndex].date)}, it's a coin flip.` : "It stays better than even for two years.";
  return [first, second];
}

type Props = { mode: "empty" } | { mode: "loading" } | { mode: "ready"; shown: Forecast };

export function Hero(props: Props) {
  const ready = props.mode === "ready";
  const lines: [string, string] = ready ? sentence(props.shown) : ["See how long", "your cash lasts."];

  return (
    <section aria-labelledby="hero-heading" className="guest-check border-b border-rule">
      <div className="mx-auto max-w-6xl 2xl:max-w-7xl px-4 pb-8 pt-9 sm:px-6 sm:pt-12">
        <h1
          id="hero-heading"
          className={`font-display text-[clamp(2.25rem,4.6vw,4rem)] font-extrabold leading-[0.98] text-ink [font-stretch:66%] [text-wrap:balance] ${
            props.mode === "loading" ? "opacity-50" : ""
          }`}
        >
          <span className="block">{lines[0]}</span>
          <span className="block">{lines[1]}</span>
        </h1>
        <p className="mt-5 max-w-[60ch] text-lg text-carbon">
          {ready ? (
            <>
              On {fullDate(props.shown.as_of)}, the day your books closed, you had{" "}
              <strong className="font-bold">{money(props.shown.cash_today_cents)}</strong> in the bank. Cash drops below one payroll
              plus rent ({money(props.shown.floor_cents)})
              {props.shown.below_floor_likely ? ` around ${monthDay(props.shown.below_floor_likely)}.` : " later than two years out."}
            </>
          ) : props.mode === "loading" ? (
            "Reading the bank and your records. Your paydays fill in as soon as the Forecaster finishes."
          ) : (
            "Start in the Chat: connect the bank and add your records. Four agents check every transaction, find what's wrong and work out when cash runs short. Nothing changes without your approval."
          )}
        </p>
        <PaydayStrip paydays={ready ? props.shown.paydays : null} />
      </div>
    </section>
  );
}
