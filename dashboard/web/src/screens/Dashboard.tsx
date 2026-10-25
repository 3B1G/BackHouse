import { useEffect, useRef, useState } from "react";
import { api, followRun, recall, remember, type Finding, type Forecast, type Plan, type Scenario, type StepEvent, type StepId } from "../api";
import { AgentsPanel } from "../components/AgentsPanel";
import { ChangesSection } from "../components/ChangesSection";
import { GetStarted } from "../components/GetStarted";
import { Hero } from "../components/Hero";
import { TrendPanel } from "../components/TrendPanel";
import { WhatIfSection } from "../components/WhatIfSection";
import { ForecastView } from "./ForecastView";
import { HowItWorks } from "./HowItWorks";

export function Dashboard({ view, onRestaurant }: { view: "dashboard" | "forecast" | "how"; onRestaurant: (name: string | null) => void }) {
  const [runId, setRunId] = useState<string | null>(
    () => new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("run") ?? recall("boh-run"),
  );
  const [steps, setSteps] = useState<Partial<Record<StepId, StepEvent>>>({});
  const [findings, setFindings] = useState<Finding[]>([]);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [finished, setFinished] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const [shown, setShown] = useState<Forecast | null>(null);
  const [scenarios, setScenarios] = useState<Scenario[] | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [whatIfError, setWhatIfError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const latest = useRef(0);

  useEffect(() => {
    if (!runId) return;
    setSteps({});
    setFindings([]);
    setPlan(null);
    setFinished(false);
    setFailed(null);
    setShown(null);
    setScenarios(null);
    setSelected([]);
    return followRun(
      runId,
      (e) => {
        if (e.type === "run") onRestaurant(e.restaurant);
        else if (e.type === "step") {
          setSteps((s) => ({ ...s, [e.step]: e }));
          if (e.forecast) setShown(e.forecast);
        } else if (e.type === "finding") setFindings((f) => [...f, e.finding]);
        else if (e.type === "plan") setPlan(e.plan);
        else if (e.type === "done") setFinished(true);
        else if (e.type === "error") setFailed(`${e.text}. Nothing was changed.`);
      },
      () => setFailed("Lost the connection to the check. Start it again to see fresh results."),
    );
  }, [runId]);

  // The what-if menu comes with each scenario's own effect, so it loads once the forecast exists.
  const haveForecast = shown !== null;
  useEffect(() => {
    if (!haveForecast || scenarios) return;
    api
      .scenarios()
      .then((r) => setScenarios(r.scenarios))
      .catch(() => setWhatIfError("The what-ifs didn't load. Reload the page to try again."));
  }, [haveForecast, scenarios]);

  const begin = (id: string, restaurant: string) => {
    remember("boh-run", id);
    onRestaurant(restaurant);
    setRunId(id);
  };

  const reset = () => {
    remember("boh-run", null);
    onRestaurant(null);
    setRunId(null);
  };

  const apply = async (next: string[]) => {
    const before = selected;
    setSelected(next);
    setPending(true);
    setWhatIfError(null);
    const ticket = ++latest.current;
    try {
      const f = await api.forecast(next);
      if (ticket === latest.current) setShown(f);
    } catch (err) {
      if (ticket === latest.current) {
        setSelected(before);
        setWhatIfError(`${(err as Error).message}`);
      }
    } finally {
      if (ticket === latest.current) setPending(false);
    }
  };

  const toggle = (id: string) => apply(selected.includes(id) ? selected.filter((s) => s !== id) : [...selected, id]);
  const bookkeeper = steps.bookkeeper;

  const whatIfs = !shown ? null : scenarios ? (
    <WhatIfSection
      scenarios={scenarios}
      selected={selected}
      f={shown}
      pending={pending}
      error={whatIfError}
      onToggle={toggle}
      onClear={() => apply([])}
    />
  ) : (
    <p className={`text-[0.9375rem] ${whatIfError ? "font-bold text-serial" : "text-pencil"}`} role={whatIfError ? "alert" : undefined}>
      {whatIfError ?? "Working out what each what-if does…"}
    </p>
  );

  if (view === "forecast")
    return <ForecastView f={shown} running={!!runId} compare={selected.length > 0} pending={pending} whatIfs={whatIfs} />;
  if (view === "how") return <HowItWorks steps={steps} running={!!runId} plan={plan} />;

  return (
    <>
      {!runId ? <Hero mode="empty" /> : shown ? <Hero mode="ready" shown={shown} /> : <Hero mode="loading" />}

      <div className="mx-auto grid max-w-6xl 2xl:max-w-7xl gap-x-10 gap-y-12 px-4 py-10 sm:px-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="min-w-0 space-y-12">
          {shown ? (
            <>
              <TrendPanel f={shown} compare={selected.length > 0} pending={pending} />
              {whatIfs}
            </>
          ) : (
            <section aria-label="Charts" className="rounded-lg border-2 border-dashed border-rule p-6">
              <p className="max-w-[56ch] text-[1.0625rem] text-carbon">
                {runId
                  ? "Your charts draw as soon as the Forecaster finishes."
                  : "Charts of your cash appear here: the past year from your bank and the next 15 months from the Forecaster, with what-ifs you can try."}
              </p>
            </section>
          )}

          <ChangesSection plan={plan} market={steps.market} step={steps.orchestrator} started={!!runId} />

          <section aria-labelledby="problems-heading" className="min-w-0">
            <h2 id="problems-heading" className="font-display text-3xl font-extrabold text-ink [font-stretch:80%]">
              Problems in your books
            </h2>
            {findings.length === 0 && (
              <div className="mt-4 rounded-lg border-2 border-dashed border-rule p-6">
                <p className="max-w-[56ch] text-[1.0625rem] text-carbon">
                  {!runId
                    ? "When the Bookkeeper finds something wrong, it hangs here with the evidence: what the bank shows next to what your records say."
                    : bookkeeper?.state === "waiting"
                      ? "The Bookkeeper hasn't checked this data yet. Its findings will hang here, with the evidence."
                      : bookkeeper?.state === "working"
                        ? "The Bookkeeper is looking into what didn't match."
                        : "Problems show up here as the Bookkeeper confirms them."}
                </p>
              </div>
            )}
          </section>
        </div>

        <aside
          className={`transition-[top] duration-200 lg:sticky lg:top-[calc(var(--header-visible,3rem)+1.5rem)] lg:self-start ${
            finished ? "" : "order-first lg:order-none"
          }`}
        >
          {!runId ? <GetStarted onStarted={begin} /> : <AgentsPanel steps={steps} finished={finished} failed={failed} onRetry={reset} />}
        </aside>
      </div>
    </>
  );
}
