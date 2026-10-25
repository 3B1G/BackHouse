import { useEffect, useState } from "react";
import type { Payday } from "../api";
import { shortDate, yearOf } from "../format";

/** How a stub looks for a given chance that payroll clears: darker red the riskier, and always the number. */
function tone(clears: number) {
  if (clears >= 0.95) return "bg-paper text-ink border-rule";
  if (clears >= 0.8) return "bg-serial-soft text-carbon border-serial-mid";
  if (clears >= 0.5) return "bg-serial-mid text-carbon border-serial-mid";
  return "bg-serial text-white border-serial";
}

/** The paydays where payroll stops being safe, and where it becomes a coin flip (indexes into the full list). */
export function markers(paydays: Payday[]) {
  const firstRisk = paydays.findIndex((p) => p.clears < 0.95);
  const safeIndex = firstRisk === -1 ? paydays.length - 1 : firstRisk - 1;
  const flipIndex = paydays.findIndex((p) => p.clears <= 0.5);
  return { safeIndex, flipIndex };
}

type Stub =
  | { kind: "group"; first: string; last: string; count: number }
  | { kind: "day"; date: string; clears: number; index: number };

/** Leading paydays that all clear become one stub; then each payday until payroll is more likely missed than made. */
function stubs(paydays: Payday[]): Stub[] {
  let lead = 0;
  while (lead < paydays.length && paydays[lead].clears >= 0.995) lead++;
  if (lead < 3) lead = 0;
  const out: Stub[] = lead ? [{ kind: "group", first: paydays[0].date, last: paydays[lead - 1].date, count: lead }] : [];
  const deep = paydays.findIndex((p) => p.clears < 0.35);
  const end = Math.min(paydays.length, deep === -1 ? lead + 14 : deep + 2);
  for (let i = lead; i < end; i++) out.push({ kind: "day", date: paydays[i].date, clears: paydays[i].clears, index: i });
  return out;
}

/** The strip "inks in" left to right once, then answers what-ifs instantly. Numbers are visible from the first frame. */
function useInking() {
  const [phase, setPhase] = useState<"plain" | "inking" | "settled">("plain");
  useEffect(() => {
    const a = window.setTimeout(() => setPhase("inking"), 80);
    const b = window.setTimeout(() => setPhase("settled"), 1400);
    return () => {
      window.clearTimeout(a);
      window.clearTimeout(b);
    };
  }, []);
  return phase;
}

const STUB = "stub flex h-24 flex-col justify-between rounded-sm border-2 px-1.5 py-1.5 transition-colors duration-300";
const PCT = "font-display text-[1.6rem] font-extrabold leading-none [font-stretch:64%]";

export function PaydayStrip({ paydays }: { paydays: Payday[] | null }) {
  const phase = useInking();

  if (!paydays) {
    return (
      <div className="mt-8">
        <p className="mb-2 text-[0.9375rem] text-pencil">Your paydays appear here, with the chance payroll clears on each one.</p>
        <ol aria-hidden="true" className="flex gap-1.5 overflow-hidden">
          {Array.from({ length: 16 }, (_, i) => (
            <li key={i} className="stub h-24 w-15 shrink-0 rounded-sm border-2 border-rule bg-paper/50" />
          ))}
        </ol>
      </div>
    );
  }

  const { safeIndex, flipIndex } = markers(paydays);
  const list = stubs(paydays);

  return (
    <figure className="mt-8">
      <figcaption id="paydays-caption" className="mb-2 text-[0.9375rem] text-pencil">
        Every other Friday is payday. Each stub is the chance payroll clears that day.
      </figcaption>
      <div className="-mx-4 overflow-x-auto px-4 pb-2 [mask-image:linear-gradient(to_right,black_88%,transparent)] sm:mx-0 sm:px-0 lg:[mask-image:none]">
        <ol aria-labelledby="paydays-caption" className="flex min-w-max snap-x gap-1.5">
          {list.map((s, i) => {
            const delay = { transitionDelay: phase === "inking" ? `${i * 45}ms` : "0ms" };
            if (s.kind === "group")
              return (
                <li key="group" className="flex w-32 shrink-0 snap-start flex-col gap-1">
                  <div style={delay} className={`${STUB} ${phase === "plain" ? "border-rule bg-paper text-ink" : tone(1)}`}>
                    <span className="text-xs font-bold leading-tight">
                      {shortDate(s.first)} to {shortDate(s.last)}
                      <span className="block font-normal">{s.count} paydays</span>
                    </span>
                    <span className={PCT}>
                      100%<span className="sr-only"> chance payroll clears on each</span>
                    </span>
                  </div>
                  <span className="min-h-4 text-[0.6875rem] font-bold leading-tight text-ink">{safeIndex < s.count ? "safe through" : "all safe"}</span>
                </li>
              );
            const pct = Math.round(s.clears * 100);
            const prev = list[i - 1];
            const showYear = !prev || prev.kind === "group" ? yearOf(s.date) !== yearOf(list[0].kind === "group" ? list[0].first : s.date) || !prev : yearOf(s.date) !== yearOf(prev.date);
            return (
              <li key={s.date} className="flex w-15 shrink-0 snap-start flex-col gap-1">
                <div style={delay} className={`${STUB} ${phase === "plain" ? "border-rule bg-paper text-ink" : tone(s.clears)}`}>
                  <span className="text-xs font-bold leading-tight">
                    {shortDate(s.date)}
                    {showYear && <span className="block font-normal">{yearOf(s.date)}</span>}
                  </span>
                  <span className={PCT}>
                    {pct}%<span className="sr-only"> chance payroll clears</span>
                  </span>
                </div>
                <span className="min-h-4 text-[0.6875rem] font-bold leading-tight text-ink">
                  {s.index === safeIndex && flipIndex !== 0 ? "safe through" : s.index === flipIndex ? "coin flip" : ""}
                </span>
              </li>
            );
          })}
        </ol>
      </div>
    </figure>
  );
}
