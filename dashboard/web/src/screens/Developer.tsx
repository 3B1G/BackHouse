import { MotionConfig } from "motion/react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import Onboarding06, { type StepCheck, type StepType, type TimelineStep } from "@/components/ui/onboarding-06";
import { api, remember } from "../api";
import { AgentMap } from "../components/AgentMap";
import { clockTime, seconds, traceKey, useNow, useTrace, type TraceEvent, type TraceKind } from "../trace";

const SAMPLE_QUESTION = "Which bills are still unpaid?";

// What each step of a check does, in the order the Orchestrator calls them.
const CHECK_STEPS = [
  { agent: "Nessie", title: "Nessie reads the bank", what: "nessie_sync.py pulls every account, purchase and deposit from Capital One's Nessie API into a local copy." },
  { agent: "Records", title: "Your records become the books", what: "build_book.py builds every cost and bill; features.py adds daily sales and dish numbers." },
  { agent: "Matcher", title: "The Matcher checks the bank against the books", what: "baseline_reconciler.py matches each bank transaction to a cost in your records." },
  {
    agent: "Bookkeeper",
    title: "The Bookkeeper explains what didn't match",
    what: "The Fetch.ai agent's findings pass through contracts.py before they can touch the forecast.",
  },
  {
    agent: "Forecaster",
    title: "The Forecaster plays out 1,000 futures",
    what: "forecaster.py simulates the next two years day by day, from the bank history, the bills and any Bookkeeper fixes the verifier passed.",
  },
  {
    agent: "Market scout",
    title: "Market scout compares your prices",
    what: "The Fetch.ai agent reads nearby restaurants' own menu pages and puts each dish below, at or above the market.",
  },
  {
    agent: "Orchestrator",
    title: "The orchestrator suggests changes",
    what: "Its brief gathers cash, bills, findings, the forecast and the prices, then forecasts each candidate change. Changes dry-runs the ones that help and saves them for your approval.",
  },
];
const LATER_STEPS = [
  {
    id: "Changes",
    title: "You approve, and Changes carries it out",
    what: "The only part that writes. It runs when you approve a suggestion on the dashboard, with your owner passphrase.",
  },
];

const checksOf = (events: TraceEvent[], agent: string): StepCheck[] =>
  events
    .filter((e) => e.agent === agent && e.kind === "check")
    .map((e) => ({ status: (e.detail?.status as StepCheck["status"]) ?? "pass", text: e.text }));

/** The latest check as a timeline: each step, live, with the checks it made. */
function checkTimeline(events: TraceEvent[], now: number) {
  let begin: TraceEvent | undefined;
  for (let i = events.length - 1; i >= 0 && !begin; i--) if (events[i].agent === "Pipeline" && events[i].kind === "call") begin = events[i];
  if (!begin) return null;
  const group = begin.group;
  const mine = events.filter((e) => e.group === group);
  const end = mine.find((e) => e.agent === "Pipeline" && (e.kind === "result" || e.kind === "error"));
  const steps: TimelineStep[] = [
    {
      id: "begin",
      type: "done",
      title: "User started a check",
      activityTime: clockTime(begin.at).slice(0, 8),
      description: `The Orchestrator takes the ${begin.text.includes("upload") ? "uploaded" : "sample"} books through each agent in turn.`,
    },
  ];
  for (const s of CHECK_STEPS) {
    const start = mine.find((e) => e.agent === s.agent && e.kind === "start");
    const stop = mine.find((e) => e.agent === s.agent && (e.kind === "done" || e.kind === "waiting" || e.kind === "error"));
    const type: StepType = stop
      ? stop.kind === "done"
        ? "done"
        : stop.kind === "waiting"
          ? "waiting"
          : "error"
      : start
        ? end
          ? "error"
          : "in progress"
        : "open";
    steps.push({
      id: s.agent,
      type,
      title: s.title,
      description: s.what,
      activityTime:
        type === "done"
          ? `took ${seconds(stop?.ms ?? 0)}`
          : type === "waiting"
            ? "waiting"
            : type === "error"
              ? "stopped"
              : type === "in progress"
                ? `running, ${Math.max(0, now - (start?.at ?? now)).toFixed(1)} s`
                : "up next",
      checks: checksOf(mine, s.agent),
    });
  }
  steps.push(
    end
      ? {
          id: "end",
          type: end.kind === "error" ? "error" : "done",
          title: end.kind === "error" ? "The check stopped" : "Check finished",
          activityTime: `after ${seconds(end.ms ?? 0)}`,
          description: end.kind === "error" ? "The step marked above says why." : "The dashboard, the forecast and the Chat now use these numbers.",
        }
      : { id: "end", type: "open", title: "Check finishes", activityTime: "in a few seconds" },
    ...LATER_STEPS.map((s) => ({ id: s.id, type: "open" as StepType, title: s.title, description: s.what, activityTime: "your call", muted: true })),
  );
  return { title: `Run ${group}`, steps };
}

function callTitle(e: TraceEvent, modelCalls: number) {
  if (e.agent === "Scope check") return "The scope check reads the question";
  if (e.agent === "Chat") return modelCalls === 1 ? "ASI:One decides what to look up" : "ASI:One reads what came back";
  if (e.agent === "Tools") return `It uses a tool: ${e.text.split("(")[0]}`;
  if (e.agent === "Forecaster") return e.text.includes("no what-ifs") ? "The Forecaster gives it today's numbers" : "The Forecaster runs a what-if";
  return `${e.agent} is called`;
}

/** The latest Chat question as a timeline: the scope check, each model call and each tool call, then the answer. */
function chatTimeline(events: TraceEvent[]) {
  let ask: TraceEvent | undefined;
  for (let i = events.length - 1; i >= 0 && !ask; i--) if (events[i].agent === "Chat" && events[i].kind === "start") ask = events[i];
  if (!ask) return null;
  const askedAt = ask.at;
  const group = ask.group;
  const mine = events.filter((e) => e.group === group);
  type Step = TimelineStep & { agent: string };
  const steps: Step[] = [
    { id: "ask", agent: "User", type: "done", title: "User asked", activityTime: clockTime(askedAt).slice(0, 8), description: ask.text.replace(/^Question: /, "") },
  ];
  const open: Record<string, Step> = {};
  let modelCalls = 0;
  let finished: TraceEvent | undefined;
  for (const e of mine) {
    if (e.agent === "Chat" && e.kind === "start") continue;
    if (e.agent === "Chat" && (e.kind === "done" || (e.kind === "error" && !open.Chat))) {
      finished = e;
      continue;
    }
    if (e.kind === "call") {
      if (e.agent === "Chat") modelCalls += 1;
      const step: Step = {
        id: traceKey(e),
        agent: e.agent,
        type: "in progress",
        title: callTitle(e, modelCalls),
        activityTime: "running now",
        description: e.agent === "Scope check" ? "One word back, IN or OUT, before the agent sees the question." : e.text,
        checks: [],
      };
      steps.push(step);
      open[e.agent] = step;
    } else if ((e.kind === "result" || e.kind === "error") && open[e.agent]) {
      const step = open[e.agent];
      const usage = e.detail?.usage as { total_tokens?: number } | undefined;
      step.type = e.kind === "error" ? "error" : "done";
      step.activityTime = `took ${seconds(e.ms ?? 0)}${usage?.total_tokens ? `, ${usage.total_tokens.toLocaleString()} tokens` : ""}`;
      if (e.agent === "Chat") step.description = e.text;
      else step.checks?.push({ status: e.kind === "error" ? "fail" : "pass", text: e.text });
      delete open[e.agent];
    } else if (e.kind === "check") {
      const target = [...steps].reverse().find((s) => s.agent === e.agent);
      target?.checks?.push({ status: (e.detail?.status as StepCheck["status"]) ?? "pass", text: e.text });
    }
  }
  steps.push(
    finished
      ? {
          id: "answer",
          agent: "Chat",
          type: finished.kind === "error" ? "error" : "done",
          title: finished.kind === "error" ? "The Chat stopped" : finished.text.startsWith("Declined") ? "Declined" : "Answered",
          activityTime: `after ${seconds(Math.round((finished.at - askedAt) * 1000))}`,
          description: finished.text,
        }
      : { id: "answer", agent: "Chat", type: "open", title: "The answer", activityTime: "on its way" },
  );
  return { title: `Question ${group}`, steps };
}

function Key({ children, label }: { children: ReactNode; label: string }) {
  return (
    <li className="flex items-center gap-2">
      <svg aria-hidden="true" width="28" height="16" className="shrink-0">
        {children}
      </svg>
      {label}
    </li>
  );
}

export function Developer() {
  const { events, live } = useTrace();
  const now = useNow(250);
  const [clearedAt, setClearedAt] = useState(0);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState<"check" | "chat" | null>(null);

  const visible = useMemo(() => events.filter((e) => e.at >= clearedAt), [events, clearedAt]);
  const check = useMemo(() => checkTimeline(visible, now), [visible, now]);
  const chat = useMemo(() => chatTimeline(visible), [visible]);

  const runCheck = async () => {
    setBusy("check");
    setNote(null);
    try {
      const { run_id } = await api.startRun({ source: "sample" });
      remember("boh-run", run_id); // the dashboard follows this check too
      setNote("Check started. The dashboard shows the same check.");
    } catch (err) {
      setNote((err as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const askChat = async () => {
    setBusy("chat");
    setNote(`Asked the Chat: "${SAMPLE_QUESTION}"`);
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: [{ role: "user", content: SAMPLE_QUESTION }] }),
      });
      await res.text(); // the answer itself lives in Chat; this page watches the calls
    } catch {
      setNote("The Chat didn't answer. Try again.");
    } finally {
      setBusy(null);
    }
  };

  const button = "min-h-11 rounded-md border-2 px-4 font-bold transition-colors duration-150 disabled:border-rule disabled:bg-check disabled:text-pencil";

  return (
    <MotionConfig reducedMotion="user">
      <div className="mx-auto max-w-6xl px-4 pb-16 pt-10 sm:px-6 2xl:max-w-7xl">
        <div className="flex flex-wrap items-end justify-between gap-x-8 gap-y-4">
          <div>
            <h1 className="font-display text-[clamp(2.25rem,4vw,3.5rem)] font-extrabold leading-none text-ink [font-stretch:66%]">Developer</h1>
            <p className="mt-3 max-w-[64ch] text-lg text-carbon">
              Watch the agents work. The map shows who calls whom as it happens; the timelines walk through the latest check and the latest
              Chat question, with every check they make.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <span role="status" className={`text-[0.9375rem] font-bold ${live ? "text-ink" : "text-serial"}`}>
              {live ? "Streaming live" : "Reconnecting…"}
            </span>
            <button type="button" onClick={() => setClearedAt(Date.now() / 1000)} className={`${button} border-rule bg-paper text-ink hover:border-ink`}>
              Clear
            </button>
            <button type="button" onClick={askChat} disabled={busy !== null} className={`${button} border-ink bg-paper text-ink hover:bg-check`}>
              {busy === "chat" ? "Asking…" : "Ask the Chat"}
            </button>
            <button type="button" onClick={runCheck} disabled={busy !== null} className={`${button} border-ink bg-ink text-white hover:bg-[#173b29]`}>
              {busy === "check" ? "Starting…" : "Run a check"}
            </button>
          </div>
        </div>
        {note && (
          <p role="status" className="mt-3 text-[0.9375rem] font-bold text-ink">
            {note}
          </p>
        )}

        <section aria-labelledby="dev-map" className="mt-10">
          <h2 id="dev-map" className="font-display text-3xl font-extrabold text-ink [font-stretch:80%]">
            Who calls whom
          </h2>
          <p className="mt-1 max-w-[72ch] text-[0.9375rem] text-pencil">
            The Orchestrator runs a check by calling six agents in order, then has Changes dry-run the fixes it suggests. The Chat agent answers questions with a scope check, ASI:One and
            read-only tools, and reaches the Forecaster through its what-if tool.
          </p>
          <AgentMap events={visible} now={now} />
          <ul className="mt-3 flex flex-wrap gap-x-6 gap-y-2 text-sm text-carbon">
            <Key label="A call going out">
              <circle cx="14" cy="8" r="6" fill="var(--color-ballpoint)" />
            </Key>
            <Key label="An answer coming back">
              <circle cx="14" cy="8" r="6" fill="var(--color-ink)" />
            </Key>
            <Key label="Working right now">
              <rect x="2" y="2" width="24" height="12" rx="4" fill="var(--color-check-deep)" stroke="var(--color-ink)" strokeWidth="2" />
            </Key>
            <Key label="The Chat's what-ifs">
              <line x1="2" x2="26" y1="8" y2="8" stroke="var(--color-ghost)" strokeWidth="2" strokeDasharray="2 4" strokeLinecap="round" />
            </Key>
          </ul>
        </section>

        <div className="mt-12 grid gap-12 lg:grid-cols-2">
          <section aria-labelledby="dev-check">
            <h2 id="dev-check" className="font-display text-3xl font-extrabold text-ink [font-stretch:80%]">
              The latest check
            </h2>
            <Onboarding06
              className="mt-2"
              title={check?.title ?? "No check yet"}
              steps={check?.steps ?? []}
              empty="Run a check and each step fills in here as it happens, with the checks it makes."
            />
          </section>
          <section aria-labelledby="dev-chat">
            <h2 id="dev-chat" className="font-display text-3xl font-extrabold text-ink [font-stretch:80%]">
              The latest Chat question
            </h2>
            <Onboarding06
              className="mt-2"
              title={chat?.title ?? "No question yet"}
              steps={chat?.steps ?? []}
              empty="Ask the Chat something and you'll see the scope check, each model call and each tool it uses."
            />
          </section>
        </div>

        <LiveLog events={visible} />
      </div>
    </MotionConfig>
  );
}

const AGENTS = ["Pipeline", "Nessie", "Records", "Matcher", "Bookkeeper", "Forecaster", "Market scout", "Orchestrator", "Changes", "Scope check", "Chat", "Tools"];
const VERB: Record<TraceKind, string> = {
  start: "started",
  done: "done",
  waiting: "waiting",
  error: "error",
  call: "called",
  log: "printed",
  result: "returned",
  check: "checked",
};

/** Characters jsPDF's built-in fonts can't draw, swapped for ones they can. */
const pdfText = (t: string) =>
  t
    .replace(/[\u2018\u2019]/g, "'")
    .replace(/[\u201C\u201D]/g, '"')
    .replace(/[\u2013\u2014\u2212]/g, "-")
    .replace(/\u2026/g, "...")
    .replace(/[^\x20-\x7E\xA0-\xFF\n]/g, "?");

const stamp = () => new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");

function download(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

const detailText = (e: TraceEvent) =>
  e.detail && Object.keys(e.detail).some((k) => k !== "step") ? JSON.stringify({ ...e.detail, step: undefined }).slice(0, 400) : "";

/** The log as a landscape PDF table, one row per event, header repeated on every page. jsPDF loads only when asked. */
async function savePdf(rows: TraceEvent[], scope: string) {
  const { jsPDF } = await import("jspdf");
  const doc = new jsPDF({ orientation: "landscape", unit: "pt", format: "letter" });
  const W = doc.internal.pageSize.getWidth();
  const H = doc.internal.pageSize.getHeight();
  const M = 36;
  const fixed = [72, 80, 56, 0, 112, 50];
  const cols = ["Time", "Agent", "What", "Event", "Group", "Took"].map((label, i) => ({ label, w: fixed[i] || W - 2 * M - 370 }));
  let y = M;
  doc.setFont("helvetica", "bold");
  doc.setFontSize(16);
  doc.setTextColor(31, 77, 54);
  doc.text("BackHouse developer log", M, y + 10);
  doc.setFont("helvetica", "normal");
  doc.setFontSize(9);
  doc.setTextColor(85, 96, 90);
  doc.text(pdfText(`Saved ${new Date().toLocaleString()}. ${rows.length} events, ${scope}.`), M, y + 26);
  y += 40;
  const header = () => {
    doc.setFillColor(31, 77, 54);
    doc.rect(M, y, W - 2 * M, 16, "F");
    doc.setFont("helvetica", "bold");
    doc.setFontSize(8.5);
    doc.setTextColor(252, 252, 250);
    let x = M + 4;
    for (const c of cols) {
      doc.text(c.label, x, y + 11);
      x += c.w;
    }
    doc.setFont("helvetica", "normal");
    y += 16;
  };
  header();
  rows.forEach((e, i) => {
    const detail = detailText(e);
    const cells = [clockTime(e.at), e.agent, VERB[e.kind], detail ? `${e.text}\n${detail}` : e.text, e.group ?? "", e.ms === null ? "" : seconds(e.ms)];
    doc.setFontSize(8);
    const lines = cells.map((c, j) => doc.splitTextToSize(pdfText(c), cols[j].w - 8) as string[]);
    const h = Math.max(...lines.map((l) => l.length)) * 10 + 6;
    if (y + h > H - M) {
      doc.addPage();
      y = M;
      header();
      doc.setFontSize(8);
    }
    if (i % 2) {
      doc.setFillColor(238, 244, 234);
      doc.rect(M, y, W - 2 * M, h, "F");
    }
    if (e.kind === "error") doc.setTextColor(179, 38, 30);
    else doc.setTextColor(34, 39, 42);
    let x = M + 4;
    lines.forEach((l, j) => {
      doc.text(l, x, y + 10);
      x += cols[j].w;
    });
    y += h;
  });
  doc.save(`backhouse-log-${stamp()}.pdf`);
}

function LiveLog({ events }: { events: TraceEvent[] }) {
  const [agent, setAgent] = useState<string | null>(null);
  const [printed, setPrinted] = useState(true);
  const [saving, setSaving] = useState(false);
  const log = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const matching = useMemo(() => events.filter((e) => (!agent || e.agent === agent) && (printed || e.kind !== "log")), [events, agent, printed]);
  const rows = matching.slice(-400);
  const scope = `${agent ? `${agent} only` : "all agents"}${printed ? "" : ", printed lines hidden"}`;
  useEffect(() => {
    const el = log.current;
    if (el && follow.current) el.scrollTop = el.scrollHeight;
  }, [matching]);

  const pdf = async () => {
    setSaving(true);
    try {
      await savePdf(matching, scope);
    } finally {
      setSaving(false);
    }
  };
  const json = () => download(new Blob([JSON.stringify(matching, null, 2)], { type: "application/json" }), `backhouse-log-${stamp()}.json`);
  const button = "min-h-11 rounded-md border-2 px-4 font-bold transition-colors duration-150 disabled:border-rule disabled:bg-check disabled:text-pencil";

  return (
    <section aria-labelledby="dev-log" className="mt-12">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 id="dev-log" className="font-display text-3xl font-extrabold text-ink [font-stretch:80%]">
            Live log
          </h2>
          <p className="mt-1 max-w-[70ch] text-[0.9375rem] text-pencil">
            Every event as it happens, newest last; the map and the timelines above are drawn from it. Saving keeps what the filters
            show.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button type="button" onClick={pdf} disabled={!matching.length || saving} className={`${button} border-ink bg-ink text-white hover:bg-[#173b29]`}>
            {saving ? "Saving…" : "Save as PDF"}
          </button>
          <button type="button" onClick={json} disabled={!matching.length} className={`${button} border-ink bg-paper text-ink hover:bg-check`}>
            Save as JSON
          </button>
        </div>
      </div>
      <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
        <div role="group" aria-label="Show events from" className="flex flex-wrap gap-2">
          {[null, ...AGENTS].map((name) => (
            <button
              key={name ?? "all"}
              type="button"
              aria-pressed={agent === name}
              onClick={() => setAgent(name)}
              className={`min-h-11 rounded-md border-2 px-3 text-sm font-bold transition-colors duration-150 ${
                agent === name ? "border-ink bg-ink text-paper" : "border-rule bg-paper text-ink hover:border-ink"
              }`}
            >
              {name ?? "All agents"}
            </button>
          ))}
        </div>
        <label className="flex min-h-11 items-center gap-2 text-[0.9375rem] text-carbon">
          <input type="checkbox" checked={printed} onChange={(e) => setPrinted(e.target.checked)} className="h-5 w-5 accent-[var(--color-ink)]" />
          Show what scripts print
        </label>
      </div>
      <div
        ref={log}
        role="log"
        aria-label="Agent events, newest last"
        tabIndex={0}
        onScroll={(e) => {
          const el = e.currentTarget;
          follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        }}
        className="mt-3 max-h-[30rem] overflow-y-auto rounded-md border border-rule bg-paper"
      >
        {rows.length === 0 ? (
          <p className="p-5 text-[1.0625rem] text-carbon">Nothing yet. Run a check or ask the Chat, and each event shows up here as it happens.</p>
        ) : (
          <ul className="divide-y divide-rule/60">
            {rows.map((e) => (
              <li
                key={traceKey(e)}
                className="grid grid-cols-[6.5rem_7.5rem_minmax(0,1fr)] gap-x-3 px-3 py-1.5 text-sm sm:grid-cols-[6.5rem_7.5rem_4.75rem_minmax(0,1fr)_4.5rem]"
              >
                <span className="tabular-nums text-pencil">{clockTime(e.at)}</span>
                <span className="truncate font-bold text-carbon">{e.agent}</span>
                <span className={`hidden sm:block ${e.kind === "error" ? "font-bold text-serial" : "text-pencil"}`}>{VERB[e.kind]}</span>
                <span
                  className={`min-w-0 break-words ${
                    e.kind === "log" ? "font-mono text-[0.8125rem] text-pencil" : e.kind === "error" ? "font-bold text-serial" : "text-carbon"
                  }`}
                >
                  {e.text}
                  {e.group && <span className="ml-2 text-pencil">{e.group}</span>}
                  {detailText(e) && (
                    <details className="mt-1">
                      <summary className="cursor-pointer text-ink underline underline-offset-2">Details</summary>
                      <pre className="mt-1 max-h-60 overflow-auto rounded bg-check p-2 font-mono text-[0.8125rem] text-carbon">
                        {JSON.stringify({ ...e.detail, step: undefined }, null, 2)}
                      </pre>
                    </details>
                  )}
                </span>
                <span className="hidden text-right tabular-nums text-pencil sm:block">{e.ms === null ? "" : seconds(e.ms)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
