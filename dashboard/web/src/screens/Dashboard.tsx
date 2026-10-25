import { useEffect, useState } from "react";
import {
  api,
  followRun,
  owner,
  recall,
  remember,
  type Finding,
  type Forecast,
  type OwnerReview,
  type Plan,
  type StepEvent,
  type StepId,
} from "../api";
import { AgentsPanel } from "../components/AgentsPanel";
import { ChangesSection, type Focus } from "../components/ChangesSection";
import { FindingsReview } from "../components/FindingsReview";
import { Hero } from "../components/Hero";
import { TrendPanel } from "../components/TrendPanel";
import { HowItWorks } from "./HowItWorks";

export function Dashboard({ view, onRestaurant }: { view: "dashboard" | "how"; onRestaurant: (name: string | null) => void }) {
  const [runId, setRunId] = useState<string | null>(
    () => new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("run") ?? recall("boh-run"),
  );
  const [steps, setSteps] = useState<Partial<Record<StepId, StepEvent>>>({});
  const [findings, setFindings] = useState<Finding[]>([]);
  const [plan, setPlan] = useState<Plan | null>(null);
  // The Chat's buttons arrive as #/?change=<dish>&price=<cents>: that change, scrolled to, with the owner's price in it.
  const [focus] = useState<Focus | null>(() => {
    const q = new URLSearchParams(window.location.hash.split("?")[1] ?? "");
    const dish = q.get("change");
    return dish ? { dish, price: Number(q.get("price")) || null } : null;
  });
  const [review, setReview] = useState<OwnerReview | null>(null);
  const [planError, setPlanError] = useState<string | null>(null);
  const [finished, setFinished] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const [shown, setShown] = useState<Forecast | null>(null);

  useEffect(() => {
    if (!runId) return;
    setSteps({});
    setFindings([]);
    setPlan(null);
    setFinished(false);
    setFailed(null);
    setShown(null);
    let started: number | null = null;
    let asked = false;
    // A check that finished a while ago (back from the Chat, a reload, a demo reset, the API restarted): the owner may
    // have signed, approved or declined since, so its saved plan is not shown; the orchestrator's plan as it is now is.
    const old = (t: number) => started !== null && Date.now() / 1000 - started - t > 5;
    const askNow = () => {
      if (asked || focus) return; // arriving from the Chat already asks
      asked = true;
      refreshPlan();
    };
    return followRun(
      runId,
      (e) => {
        if (e.type === "run") {
          onRestaurant(e.restaurant);
          started = e.started_at ?? null;
        } else if (e.type === "step") {
          setSteps((s) => ({ ...s, [e.step]: e }));
          if (e.forecast) setShown(e.forecast);
        } else if (e.type === "finding") setFindings((f) => [...f, e.finding]);
        else if (e.type === "plan") {
          if (old(e.t)) askNow();
          else setPlan(e.plan);
        } else if (e.type === "done") {
          setFinished(true);
          if (old(e.t)) askNow();
        } else if (e.type === "error") setFailed(`${e.text}. Nothing was changed.`);
      },
      () => setFailed("Lost the connection to the check. Start it again to see fresh results."),
    );
  }, [runId]);

  // The owner's review of the Bookkeeper's findings comes with each brief: it says what the owner can decide now.
  useEffect(() => {
    if (!plan) return;
    owner
      .review()
      .then(setReview)
      .catch(() => setReview(null));
  }, [plan]);

  // After the owner signs a review (or an older check reopens): the orchestrator's suggestions as they are now, and the
  // forecast redrawn on the same inputs the orchestrator and the Chat use, so every date on the page agrees.
  const refreshPlan = () => {
    setPlanError(null);
    owner
      .plan()
      .then(setPlan)
      .catch((err) => setPlanError((err as Error).message));
    api
      .forecast()
      .then(setShown)
      .catch(() => {}); // the check's own forecast stays on screen
  };

  // Arriving from the Chat: the latest suggestions, even when the check that made them is gone.
  useEffect(() => {
    if (focus) refreshPlan();
  }, [focus]);

  // #/?section=dash-cash (the Chat's "See it on the dashboard" buttons): scroll to that part once it's on the page, and
  // outline it for a moment.
  useEffect(() => {
    let timer: number | undefined;
    const go = () => {
      window.clearInterval(timer);
      const id = new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("section");
      if (!id?.startsWith("dash-")) return;
      let tries = 0;
      let last: number | null = null;
      timer = window.setInterval(() => {
        const el = document.getElementById(id);
        // The charts and the review draw as the check replays and push the sections down: wait until it holds still.
        const top = el ? Math.round(el.getBoundingClientRect().top + window.scrollY) : null;
        const settled = top !== null && top === last;
        last = top;
        if (!settled && ++tries < 48) return;
        window.clearInterval(timer);
        window.history.replaceState(null, "", "#/");
        if (!el) return;
        const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        const animate = !reduce && document.visibilityState === "visible"; // a background page doesn't run smooth scrolling
        el.scrollIntoView({ behavior: animate ? "smooth" : "auto", block: "start" });
        el.style.outline = "3px solid var(--color-serial)";
        el.style.outlineOffset = "8px";
        el.style.borderRadius = "12px";
        window.setTimeout(() => {
          el.style.outline = "";
          el.style.outlineOffset = "";
        }, 2200);
      }, 125);
    };
    go();
    window.addEventListener("hashchange", go);
    return () => {
      window.removeEventListener("hashchange", go);
      window.clearInterval(timer);
    };
  }, []);

  const reset = () => {
    remember("boh-run", null);
    onRestaurant(null);
    setRunId(null);
  };

  const bookkeeper = steps.bookkeeper;

  if (view === "how") return <HowItWorks steps={steps} running={!!runId} plan={plan} />;

  return (
    <>
      <div id="dash-paydays" className="scroll-mt-16">
        {!runId ? <Hero mode="empty" /> : shown ? <Hero mode="ready" shown={shown} /> : <Hero mode="loading" />}
      </div>

      <div className="mx-auto grid max-w-6xl 2xl:max-w-7xl gap-x-10 gap-y-12 px-4 py-10 sm:px-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="min-w-0 space-y-12">
          <div id="dash-cash" className="scroll-mt-28">
            {shown ? (
              <TrendPanel f={shown} />
            ) : (
              <section aria-label="Charts" className="rounded-lg border-2 border-dashed border-rule p-6">
                <p className="max-w-[56ch] text-[1.0625rem] text-carbon">
                  {runId
                    ? "Your charts draw as soon as the Forecaster finishes."
                    : "Charts of your cash appear here: the past year from your bank and the next 15 months from the Forecaster. Ask the Chat a what-if to see how a change would move them."}
                </p>
              </section>
            )}
          </div>

          <section id="dash-problems" aria-labelledby="problems-heading" className="min-w-0 scroll-mt-28">
            <h2 id="problems-heading" className="font-display text-3xl font-extrabold text-ink [font-stretch:80%]">
              Problems in your books
            </h2>
            {review && review.findings.length > 0 ? (
              <FindingsReview review={review} onReview={setReview} onSigned={refreshPlan} />
            ) : (
              <div className="mt-4 rounded-lg border-2 border-dashed border-rule p-6">
                <p className="max-w-[56ch] text-[1.0625rem] text-carbon">
                  {!runId
                    ? "When the Bookkeeper finds something wrong, it hangs here with the evidence: what the bank shows next to what your records say."
                    : bookkeeper?.state === "waiting"
                      ? "The Bookkeeper hasn't checked this data yet. Its findings will hang here, with the evidence."
                      : bookkeeper?.state === "working"
                        ? "The Bookkeeper is looking into what didn't match."
                        : findings.length
                          ? "Loading the Bookkeeper's findings for your review…"
                          : "Problems show up here as the Bookkeeper confirms them."}
                </p>
              </div>
            )}
            {runId && (
              <p className="mt-4 text-[0.9375rem] text-pencil">
                New bank transactions since?{" "}
                <a href={`#/chat?ask=${encodeURIComponent("Run the Bookkeeper")}`} className="font-bold text-ink underline underline-offset-4">
                  Ask the Chat to run the Bookkeeper
                </a>
                .
              </p>
            )}
          </section>

          <div id="dash-changes" className="scroll-mt-28">
            <ChangesSection
              plan={plan}
              market={steps.market}
              step={steps.orchestrator}
              started={!!runId}
              ownerKey={review?.owner_key ?? false}
              onOwnerKey={() => setReview((r) => (r ? { ...r, owner_key: true } : r))}
              focus={focus}
            />
          </div>
          {planError && (
            <p role="alert" className="-mt-8 text-[0.9375rem] font-bold text-serial">
              {planError}
            </p>
          )}
        </div>

        <aside
          className={`transition-[top] duration-200 lg:sticky lg:top-[calc(var(--header-visible,3rem)+1.5rem)] lg:self-start ${
            finished ? "" : "order-first lg:order-none"
          }`}
        >
          {runId && <AgentsPanel steps={steps} finished={finished} failed={failed} onRetry={reset} />}
        </aside>
      </div>
    </>
  );
}
