import { useState } from "react";
import type { StepEvent, StepId } from "../api";
import { clock } from "../format";

const STEPS: { id: StepId; name: string; upNext: string }[] = [
  { id: "bank", name: "Nessie", upNext: "Reads the bank" },
  { id: "records", name: "Your records", upNext: "Builds your books" },
  { id: "matcher", name: "Matcher", upNext: "Matches the bank to your records" },
  { id: "bookkeeper", name: "Bookkeeper", upNext: "Looks into what doesn't match" },
  { id: "forecast", name: "Forecaster", upNext: "Runs 1,000 futures" },
];

function Icon({ state }: { state: "done" | "working" | "waiting" | "next" }) {
  if (state === "done")
    return (
      <svg aria-hidden="true" viewBox="0 0 24 24" className="h-6 w-6 shrink-0 text-ink" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="10" fill="var(--color-check)" />
        <path d="M7.5 12.5l3 3 6-6.5" />
      </svg>
    );
  if (state === "working")
    return (
      <svg aria-hidden="true" viewBox="0 0 24 24" className="h-6 w-6 shrink-0 animate-spin text-ink [animation-duration:1.4s]" fill="none" strokeWidth="2.5">
        <circle cx="12" cy="12" r="9.5" stroke="var(--color-rule)" />
        <path d="M12 2.5a9.5 9.5 0 0 1 9.5 9.5" stroke="currentColor" strokeLinecap="round" />
      </svg>
    );
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" className="h-6 w-6 shrink-0" fill="none" strokeWidth="2">
      <circle cx="12" cy="12" r="9.5" stroke={state === "waiting" ? "var(--color-serial)" : "var(--color-rule)"} strokeDasharray={state === "waiting" ? "3 3" : undefined} />
    </svg>
  );
}

const WORD = { done: "Done", working: "Working", waiting: "Waiting", next: "Up next" } as const;

export function AgentsPanel({
  steps,
  finished,
  failed,
  onRetry,
}: {
  steps: Partial<Record<StepId, StepEvent>>;
  finished: boolean;
  failed: string | null;
  onRetry: () => void;
}) {
  const [open, setOpen] = useState(true);
  const last = Math.max(0, ...Object.values(steps).map((s) => s?.t ?? 0));
  const heading = failed ? "The check stopped" : finished ? `Books checked in ${Math.max(1, Math.round(last))} s` : "Checking the books";
  const expanded = open || !finished || !!failed;

  return (
    <section aria-labelledby="agents-heading" className="rounded-lg border-2 border-rule bg-paper p-5">
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <h2 id="agents-heading" className="font-display text-2xl font-extrabold text-ink [font-stretch:80%]">
          {heading}
        </h2>
        {finished && !failed && (
          <button
            type="button"
            aria-expanded={expanded}
            aria-controls="agents-steps"
            onClick={() => setOpen(!open)}
            className="min-h-11 whitespace-nowrap rounded-md px-3 text-sm font-bold text-ink underline underline-offset-4 hover:bg-check"
          >
            {expanded ? "Hide steps" : "Show steps"}
          </button>
        )}
      </div>
      {expanded && (
        <ol id="agents-steps" aria-live="polite" className="mt-3">
          {STEPS.map((s, i) => {
            const e = steps[s.id];
            const state = e ? e.state : "next";
            return (
              <li key={s.id} className={`flex gap-3 py-3 ${i ? "border-t border-dashed border-rule" : ""}`}>
                <Icon state={state} />
                <div className="min-w-0 flex-1">
                  <p className="flex flex-wrap items-baseline gap-x-2">
                    <span className="font-bold">{s.name}</span>
                    <span className={`text-sm ${state === "waiting" ? "font-bold text-serial" : "text-pencil"}`}>{WORD[state]}</span>
                    {e && state !== "working" && <span className="ml-auto text-sm text-pencil">{clock(e.t)}</span>}
                  </p>
                  <p className="text-[0.9375rem] text-carbon">{e ? e.text : s.upNext}</p>
                </div>
              </li>
            );
          })}
        </ol>
      )}
      {failed && (
        <div className="mt-3 border-t border-rule pt-3">
          <p role="alert" className="text-[0.9375rem] font-bold text-serial">
            {failed}
          </p>
          <button type="button" onClick={onRetry} className="mt-3 min-h-11 rounded-md border-2 border-ink px-4 text-[0.9375rem] font-bold text-ink hover:bg-check">
            Start again
          </button>
        </div>
      )}
    </section>
  );
}
