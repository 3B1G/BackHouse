import type { ReactNode } from "react";
import type { Forecast } from "../api";
import { TrendPanel } from "../components/TrendPanel";
import { fullDate } from "../format";

function summary(f: Forecast) {
  const { bad, likely, good } = f.runout;
  const lead = f.what_if.length ? "With your what-ifs" : "If nothing changes";
  if (!likely) return `${lead}, cash likely lasts past the next two years.`;
  const cases = bad && good ? ` The bad case is ${fullDate(bad)}; the good case, ${fullDate(good)}.` : "";
  return `${lead}, cash likely runs out ${fullDate(likely)}.${cases}`;
}

/** The analysis page: the charts at full width, the what-ifs, and the Forecaster's assumptions in its own words. */
export function ForecastView({
  f,
  running,
  compare,
  pending,
  whatIfs,
}: {
  f: Forecast | null;
  running: boolean;
  compare: boolean;
  pending: boolean;
  whatIfs: ReactNode;
}) {
  return (
    <div className="mx-auto max-w-6xl px-4 pb-16 pt-10 sm:px-6 2xl:max-w-7xl">
      <h1 className="font-display text-[clamp(2.25rem,4vw,3.5rem)] font-extrabold leading-none text-ink [font-stretch:66%]">Forecast</h1>
      <p aria-live="polite" className="mt-3 max-w-[62ch] text-lg text-carbon">
        {f
          ? summary(f)
          : running
            ? "The Forecaster is still running. This page fills in as soon as it finishes."
            : "The forecast appears once your books are checked."}
      </p>
      {!f ? (
        !running && (
          <a
            href="#/"
            className="mt-5 inline-flex min-h-11 items-center rounded-md bg-ink px-5 font-bold text-white no-underline transition-colors duration-150 hover:bg-[#173b29]"
          >
            Start a check
          </a>
        )
      ) : (
        <div className="mt-10 space-y-12">
          <TrendPanel f={f} compare={compare} pending={pending} />
          {whatIfs}
          <section aria-labelledby="assumptions-heading">
            <h2 id="assumptions-heading" className="font-display text-3xl font-extrabold text-ink [font-stretch:80%]">
              What the forecast assumes
            </h2>
            <p className="mt-1 max-w-[62ch] text-[0.9375rem] text-pencil">
              In the Forecaster's own words. A what-if changes one of these for a moment; Chat can explain any of them.
            </p>
            <ul className="mt-4 max-w-[75ch] space-y-2.5 border-l-2 border-rule pl-4 text-[1.0625rem] text-carbon">
              {f.assumptions.map((a) => (
                <li key={a}>{a}</li>
              ))}
            </ul>
          </section>
        </div>
      )}
    </div>
  );
}
