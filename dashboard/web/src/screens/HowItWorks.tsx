import HowItWorksRail, { type Stamp, type TicketStep } from "@/components/ui/how-it-works";
import type { StepEvent, StepId } from "../api";
import { clock } from "../format";

type Steps = Partial<Record<StepId, StepEvent>>;
const NEXT: Stamp = { tone: "next", text: "Next" };

/** What the current check says about one step, as the check's status line and stamp. */
function live(steps: Steps, id: StepId, running: boolean): Pick<TicketStep, "status" | "stamp"> {
  const s = steps[id];
  if (!s) return { status: running ? "Up next in this check" : "Runs when you start a check" };
  if (s.state === "done") return { status: `Done at ${clock(s.t)}. ${s.text}`, stamp: { tone: "done", text: "Done" } };
  if (s.state === "waiting") return { status: s.text, stamp: { tone: "waiting", text: "Waiting" } };
  return { status: s.text, stamp: { tone: "working", text: "Working" } };
}

export function HowItWorks({ steps, running }: { steps: Steps; running: boolean }) {
  // Stage 1, "what is going on?", runs in every check; stage 2, "what do we do about it?", is the team's next piece.
  const first: TicketStep[] = [
    {
      title: "Nessie reads the bank",
      agent: "Nessie",
      uses: "Capital One's bank API",
      description: "Every transaction in the restaurant's accounts, copied and checked against the bank.",
      ...live(steps, "bank", running),
    },
    {
      title: "Your records become the books",
      agent: "Records",
      uses: "Your books export",
      description: "Every bill, invoice, payroll run and lease, with what's still unpaid.",
      ...live(steps, "records", running),
    },
    {
      title: "The Matcher checks the bank against the books",
      agent: "Matcher",
      uses: "Rules, no AI",
      description: "Each bank transaction is matched to a cost in your records, and anything that doesn't fit goes to the Bookkeeper.",
      ...live(steps, "matcher", running),
    },
    {
      title: "The Bookkeeper explains what didn't match",
      agent: "Bookkeeper",
      uses: "Fetch.ai agent on ASI:One",
      description: "It looks into each flag with 11 read-only tools, and a verifier checks its findings before they count.",
      ...live(steps, "bookkeeper", running),
    },
    {
      title: "The Forecaster plays out 1,000 futures",
      agent: "Forecaster",
      uses: "Monte Carlo simulation",
      description: "Two years, day by day, from your records with the Bookkeeper's checked fixes, to find when cash runs short.",
      ...live(steps, "forecast", running),
    },
    {
      title: "Market scout compares your prices",
      agent: "Market scout",
      uses: "Fetch.ai agent",
      description: "Your dish prices against nearby restaurants' own menus: below, at or above the market.",
      status: "Built; joins the check next",
      stamp: NEXT,
    },
  ];
  const then: TicketStep[] = [
    {
      title: "The orchestrator suggests changes",
      agent: "Orchestrator",
      uses: "ASI:One by Fetch.ai",
      description: "It weighs the findings, the forecast and the prices, and suggests a few changes, each with what it does to your cash.",
      status: "Coming next",
      stamp: NEXT,
    },
    {
      title: "You approve, and Changes carries it out",
      agent: "Changes",
      uses: "Plain code, no AI",
      description: "Nothing happens until you sign. Then it changes a price or moves a bill, and logs what it did.",
      status: "Built; joins the dashboard next",
      stamp: NEXT,
    },
    {
      title: "Ask the Chat anything",
      agent: "Chat agent",
      uses: "ASI:One by Fetch.ai",
      description: "Questions about your cash, a bill or a what-if, answered from the same bank and records. It can't change anything.",
      status: "Open it from the menu, any time",
      stamp: { tone: "ready", text: "Ready" },
      href: "#/chat",
    },
  ];
  const h2 = "mx-auto max-w-6xl px-4 font-display text-3xl font-extrabold text-ink [font-stretch:80%] sm:px-6 2xl:max-w-7xl";

  return (
    <div className="pb-16 pt-10">
      <div className="mx-auto max-w-6xl px-4 sm:px-6 2xl:max-w-7xl">
        <h1 className="font-display text-[clamp(2.25rem,4vw,3.5rem)] font-extrabold leading-none text-ink [font-stretch:66%]">How it works</h1>
        <p className="mt-3 max-w-[62ch] text-lg text-carbon">
          Each part reads your data and hands what it found to the next. Only one part can change anything, and only after you approve.
        </p>
      </div>
      <section aria-labelledby="how-first" className="mt-8">
        <h2 id="how-first" className={h2}>
          First: what is going on?
        </h2>
        <HowItWorksRail steps={first} />
      </section>
      <section aria-labelledby="how-then" className="mt-4">
        <h2 id="how-then" className={h2}>
          Then: what do you do about it?
        </h2>
        <HowItWorksRail steps={then} start={first.length + 1} />
      </section>
    </div>
  );
}
