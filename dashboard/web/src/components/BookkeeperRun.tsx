import { IconAlertTriangle, IconArrowUpRight, IconCheck, IconPlayerPlay, IconRefresh } from "@tabler/icons-react";
import { Fragment, useEffect, useState } from "react";
import { bookkeeper, type BookkeeperCoverage, type BookkeeperJob } from "../api";
import { mediumDate } from "../format";
import { ThinkingOrb } from "./ui/thinking-orb";

const POLL_MS = 4000;
const button = "inline-flex min-h-11 items-center gap-1.5 rounded-md border-2 px-4 text-[0.9375rem] font-bold transition-colors duration-150";
const at = (seconds: number) => new Date(seconds * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
// What the orb shows while it works: a quick look for new bank days, or a real run (the Bookkeeper's own steps).
const QUICK = ["Checking the newest bank day", "Comparing it with what it has checked"];
const FULL = ["Reading the bank", "Matching your records", "Looking into what doesn't match", "The Verifier checks each finding"];
const SETTLED = { up_to_date: "Up to date", new_report: "New report ready", failed: "Stopped" } as const;

function since(seconds: number, now: number) {
  const s = Math.max(0, Math.round(now / 1000 - seconds));
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min`;
}

/** What a check would do, from how far the Bookkeeper has checked. */
function plan(c: BookkeeperCoverage) {
  if (!c.available)
    return "It hasn't made a report for this data yet. Its first run reads a year of bank transactions with ASI:One, which can take up to an hour, so it starts from the command line: orchestrator bookkeeper-setup.";
  if (c.days_behind)
    return `It has checked your bank through ${mediumDate(c.covered_through!)}, but your bank goes to ${mediumDate(c.latest_bank_date!)}: ${c.days_behind} day${c.days_behind === 1 ? "" : "s"} it hasn't seen. Running it re-checks the books with ASI:One and its Verifier, which can take up to 20 minutes.`;
  return `It has checked every bank day, through ${mediumDate(c.covered_through!)}. Checking again looks for new transactions first: with none, it's done at once and spends nothing.`;
}

/** "Run the Bookkeeper": the card the Chat puts under its answer, where the owner starts the orchestrator's own update.
 * When the Bookkeeper has already checked every bank day it answers "up to date" at once and spends nothing; a run that
 * would spend ASI:One asks first. While it works, the thinking orb; then how it went, word by word. */
export function BookkeeperRun() {
  const [coverage, setCoverage] = useState<BookkeeperCoverage | null>(null);
  const [job, setJob] = useState<BookkeeperJob>({ state: "idle" });
  const [asking, setAsking] = useState(false);
  const [starting, setStarting] = useState(false); // the server asks the orchestrator first: a second or two
  const [orb, setOrb] = useState(false); // the thinking orb is on, from the click until it has shown how it went
  const [quick, setQuick] = useState(true);
  const [fresh, setFresh] = useState(0); // a result the orb just handed over: its words come in one by one
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const load = () =>
    bookkeeper.status().then((s) => {
      setCoverage(s.coverage);
      setJob(s.run);
      return s;
    });

  useEffect(() => {
    load()
      .then((s) => {
        if (s.run.state !== "running") return;
        setQuick(s.coverage?.days_behind === 0); // a run already under way when the card opens
        setOrb(true);
      })
      .catch(() => {}); // no orchestrator, no card
  }, []);

  // While it runs: a ticking clock, and the server asked again (soon at first: "up to date" takes a second).
  useEffect(() => {
    if (job.state !== "running") return;
    let timer: number | undefined;
    const tick = window.setInterval(() => setNow(Date.now()), 1000);
    const poll = (wait: number) => {
      timer = window.setTimeout(() => {
        load()
          .then((s) => s.run.state === "running" && poll(POLL_MS))
          .catch(() => poll(POLL_MS));
      }, wait);
    };
    poll(1200);
    return () => {
      window.clearTimeout(timer);
      window.clearInterval(tick);
    };
  }, [job.state]);

  const start = async () => {
    setAsking(false);
    setError(null);
    setQuick(!!coverage?.available && coverage.days_behind === 0);
    setStarting(true);
    setOrb(true);
    try {
      setJob((await bookkeeper.run()).run);
    } catch (err) {
      setError((err as Error).message);
      setOrb(false);
    } finally {
      setStarting(false);
    }
  };

  if (!coverage && job.state === "idle") return null;
  const upToDate = !!coverage?.available && coverage.days_behind === 0;
  const running = job.state === "running";
  const failed = job.state === "failed";
  const settled = job.result ? SETTLED[job.result] : "Done";

  return (
    <div className="mt-4 max-w-[62ch] rounded-lg border-2 border-rule bg-check/40 p-4">
      <h3 className="font-display text-xl font-extrabold text-ink [font-stretch:80%]">Run the Bookkeeper</h3>
      {coverage && !orb && <p className="mt-1 text-[0.9375rem] text-carbon">{plan(coverage)}</p>}

      {orb ? (
        <div className="mt-3 flex flex-col items-center rounded-md bg-paper px-4 py-5">
          <ThinkingOrb
            outcome={starting || running ? "thinking" : failed ? "failed" : "done"}
            labels={quick ? QUICK : FULL}
            doneLabel={settled}
            onSettled={() => {
              setOrb(false);
              setFresh((f) => f + 1);
            }}
          />
          <p className="mt-1 text-center text-sm text-pencil" aria-live="polite">
            {running && job.started_at ? `Started ${at(job.started_at)}, ${since(job.started_at, now)} ago.` : starting ? "Starting…" : `${settled}.`}
            {!quick && running ? " You can keep using Hermes: its findings come up on the dashboard when it's done." : ""}
          </p>
        </div>
      ) : running ? (
        <p className="mt-3 text-[0.9375rem] font-bold text-ink" aria-live="polite">
          The Bookkeeper is running (started {at(job.started_at!)}, {since(job.started_at!, now)} ago).
        </p>
      ) : (
        coverage?.available && (
          <>
            <div className="mt-3 flex flex-wrap gap-2">
              <button
                type="button"
                disabled={starting}
                onClick={() => (upToDate ? void start() : setAsking(true))}
                className={`${button} border-ink bg-ink text-paper hover:bg-ink/90 disabled:opacity-60`}
              >
                {upToDate ? <IconRefresh aria-hidden="true" size={18} /> : <IconPlayerPlay aria-hidden="true" size={18} />}
                {upToDate ? "Check for new transactions" : "Run the Bookkeeper"}
              </button>
            </div>
            {asking && (
              <div role="group" aria-labelledby="bk-ask" className="mt-3 rounded-md border-2 border-ink bg-paper p-4">
                <p id="bk-ask" className="text-[0.9375rem] text-carbon">
                  This starts a real run: the Bookkeeper re-checks the books with ASI:One, then its Verifier checks every finding. It can take up to 20
                  minutes and uses ASI:One credits. Today's findings stay until it's done; then you review the new ones and sign again.
                </p>
                <div className="mt-3 flex flex-wrap gap-2">
                  <button type="button" onClick={() => void start()} className={`${button} border-ink bg-ink text-paper hover:bg-ink/90`}>
                    Yes, run it
                  </button>
                  <button
                    type="button"
                    onClick={() => setAsking(false)}
                    className={`${button} border-transparent text-ink underline underline-offset-4 hover:bg-check`}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </>
        )
      )}

      {!orb && !running && !starting && job.state !== "idle" && job.note && (
        <p
          className={`mt-3 flex items-start gap-2 text-[0.9375rem] font-bold ${failed ? "text-serial" : "text-ink"}`}
          role={failed ? "alert" : undefined}
          aria-live="polite"
        >
          {failed ? (
            <IconAlertTriangle aria-hidden="true" size={20} className="mt-0.5 shrink-0" />
          ) : (
            <IconCheck aria-hidden="true" size={20} className="mt-0.5 shrink-0" />
          )}
          <span key={fresh}>
            {job.finished_at ? `${at(job.finished_at)}: ` : ""}
            {fresh
              ? job.note.split(" ").map((word, i) => (
                  <Fragment key={i}>
                    <span className="inline-block animate-[thinking-in_0.32s_ease-out_both]" style={{ animationDelay: `${i * 35}ms` }}>
                      {word}
                    </span>{" "}
                  </Fragment>
                ))
              : job.note}
          </span>
        </p>
      )}
      {!orb && job.result === "new_report" && (
        <a
          href="#/?section=dash-problems"
          className="mt-3 inline-flex min-h-11 items-center gap-1.5 rounded-md bg-ink px-4 text-[0.9375rem] font-bold text-paper no-underline hover:bg-ink/90"
        >
          Review the findings on the dashboard
          <IconArrowUpRight aria-hidden="true" size={18} />
        </a>
      )}
      {error && (
        <p role="alert" className="mt-3 text-[0.9375rem] font-bold text-serial">
          {error}
        </p>
      )}
    </div>
  );
}
