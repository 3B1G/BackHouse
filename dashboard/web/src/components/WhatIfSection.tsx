import type { Forecast, Scenario } from "../api";
import { fullDate, span } from "../format";

function Box({ on }: { on: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={`mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-[3px] border-2 transition-colors duration-150 ${
        on ? "border-ink bg-ink" : "border-ink/60 bg-paper"
      }`}
    >
      {on && (
        <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" fill="none" stroke="#fff" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M3.5 8.5l3 3 6-7" />
        </svg>
      )}
    </span>
  );
}

/** How far one what-if moves the day cash runs out, in words. */
function effect(days: number | null) {
  if (days === null) return null;
  if (Math.abs(days) < 3) return { text: "Barely moves it", tone: "text-pencil" };
  return { text: `${span(days)} ${days < 0 ? "sooner" : "later"}`, tone: days < 0 ? "text-serial" : "text-ink" };
}

/** Counts the what-ifs this forecast actually ran, not the boxes just ticked, so the words always match the numbers. */
function outcome(f: Forecast) {
  const count = f.what_if.length;
  const when = f.runout.likely ? `cash likely runs out ${fullDate(f.runout.likely)}` : "cash likely lasts past the next two years";
  if (count === 0) return `If nothing changes, ${when}.`;
  const days = f.vs_base?.avg_days ?? null;
  const shift =
    days === null || Math.abs(days) < 3
      ? "about the same as doing nothing"
      : `about ${span(days)} ${days < 0 ? "sooner" : "later"} than doing nothing`;
  return `With ${count === 1 ? "that" : `these ${count}`}, ${when}: ${shift}.`;
}

const GROUPS = [
  { id: "risk", title: "If something goes wrong" },
  { id: "change", title: "If you make a change" },
] as const;

export function WhatIfSection({
  scenarios,
  selected,
  f,
  pending,
  error,
  onToggle,
  onClear,
}: {
  scenarios: Scenario[];
  selected: string[];
  f: Forecast;
  pending: boolean;
  error: string | null;
  onToggle: (id: string) => void;
  onClear: () => void;
}) {
  return (
    <section aria-labelledby="whatif-heading" className="rounded-xl bg-check px-4 py-6 sm:px-6">
      <h2 id="whatif-heading" className="font-display text-3xl font-extrabold text-ink [font-stretch:80%]">
        Try a what-if
      </h2>
      <p className="mt-1 max-w-[62ch] text-[0.9375rem] text-pencil">
        Each one shows how far it moves the day cash runs out. Mix any of them: the headline, paydays and charts update together.
      </p>

      <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border-2 border-ink bg-paper px-4 py-3">
        <p aria-live="polite" aria-busy={pending} className="min-w-0 flex-1 text-lg text-carbon">
          {outcome(f)}
        </p>
        {selected.length > 0 && (
          <button
            type="button"
            onClick={onClear}
            className="min-h-11 rounded-md px-3 text-sm font-bold text-ink underline underline-offset-4 hover:bg-check"
          >
            Clear all
          </button>
        )}
      </div>
      {error && (
        <p role="alert" className="mt-3 font-bold text-serial">
          {error}
        </p>
      )}

      <div className="mt-6 grid gap-6 md:grid-cols-2">
        {GROUPS.map((g) => (
          <div key={g.id} role="group" aria-labelledby={`whatif-${g.id}`}>
            <h3 id={`whatif-${g.id}`} className="text-[0.9375rem] font-bold text-ink">
              {g.title}
            </h3>
            <ul className="mt-2 space-y-2">
              {scenarios
                .filter((s) => s.group === g.id)
                .map((s) => {
                  const on = selected.includes(s.id);
                  const e = effect(s.effect_days);
                  return (
                    <li key={s.id}>
                      <button
                        type="button"
                        aria-pressed={on}
                        disabled={!s.available}
                        onClick={() => onToggle(s.id)}
                        className={`flex min-h-14 w-full flex-wrap items-start gap-x-3 gap-y-1 rounded-md border-2 px-3 py-2.5 text-left transition-colors duration-150 disabled:opacity-60 ${
                          on ? "border-ink bg-check-deep" : "border-rule bg-paper enabled:hover:border-ink"
                        }`}
                      >
                        <Box on={on} />
                        <span className="min-w-0 flex-1">
                          <span className="block text-[0.9375rem] font-bold text-carbon">{s.title}</span>
                          <span className="block text-sm text-pencil">{s.detail}</span>
                        </span>
                        {e && (
                          <span className={`basis-full pl-8 text-sm font-bold sm:basis-auto sm:pl-0 sm:text-right ${e.tone}`}>
                            <span className="sr-only">Cash runs out </span>
                            {e.text}
                          </span>
                        )}
                      </button>
                    </li>
                  );
                })}
            </ul>
          </div>
        ))}
      </div>

      <p className="mt-5 text-[0.9375rem] text-carbon">
        Have another what-if in mind?{" "}
        <a href="#/chat" className="font-bold underline underline-offset-4">
          Ask in Chat
        </a>
      </p>
    </section>
  );
}
