import { IconAlertTriangle, IconCheck, IconPlayerPlay, IconRefresh } from "@tabler/icons-react";
import { useEffect, useState } from "react";
import { bookkeeper, type BookkeeperCoverage, type BookkeeperJob } from "../api";
import { mediumDate } from "../format";

const POLL_MS = 4000;
const button = "inline-flex min-h-11 items-center gap-1.5 rounded-md border-2 px-4 text-[0.9375rem] font-bold transition-colors duration-150";
const at = (seconds: number) => new Date(seconds * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

function since(seconds: number, now: number) {
  const s = Math.max(0, Math.round(now / 1000 - seconds));
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min`;
}

/** What a run would do, from how far the Bookkeeper has checked. */
function plan(c: BookkeeperCoverage) {
  if (!c.available)
    return "It hasn't checked this data yet. Its first run reads a year of bank transactions with ASI:One, then its Verifier checks every finding: that can take up to an hour.";
  if (c.days_behind)
    return `It has checked your bank through ${mediumDate(c.covered_through!)}, but your bank goes to ${mediumDate(c.latest_bank_date!)}: ${c.days_behind} day${c.days_behind === 1 ? "" : "s"} it hasn't seen. A run re-checks the books with ASI:One and its Verifier, which can take up to 20 minutes.`;
  return `It has checked every bank day, through ${mediumDate(c.covered_through!)}. Running it again looks for new transactions first: with none, it's done at once and spends nothing. With new ones, it re-checks the books with ASI:One and its Verifier, and you review what it finds here.`;
}

type Ask = "run" | "full" | null;

/** "Run the Bookkeeper": the orchestrator's own update, started by the owner. When the Bookkeeper has already checked
 * every bank day it answers "up to date" at once and spends nothing; a run that would spend ASI:One asks first. A new
 * report's findings then come up for review above (onFinished refreshes them). */
export function BookkeeperRun({ onFinished }: { onFinished: () => void }) {
  const [coverage, setCoverage] = useState<BookkeeperCoverage | null>(null);
  const [job, setJob] = useState<BookkeeperJob>({ state: "idle" });
  const [asking, setAsking] = useState<Ask>(null);
  const [starting, setStarting] = useState(false); // the server asks the orchestrator first: a second or two
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const load = () =>
    bookkeeper.status().then((s) => {
      setCoverage(s.coverage);
      setJob(s.run);
      return s.run;
    });

  useEffect(() => {
    load().catch(() => {}); // no orchestrator, no panel
  }, []);

  // While it runs: a ticking clock, and the server asked again (soon at first: "up to date" takes a second).
  useEffect(() => {
    if (job.state !== "running") return;
    let timer: number | undefined;
    const tick = window.setInterval(() => setNow(Date.now()), 1000);
    const poll = (wait: number) => {
      timer = window.setTimeout(() => {
        load()
          .then((run) => (run.state === "running" ? poll(POLL_MS) : onFinished()))
          .catch(() => poll(POLL_MS));
      }, wait);
    };
    poll(1200);
    return () => {
      window.clearTimeout(timer);
      window.clearInterval(tick);
    };
  }, [job.state]); // eslint-disable-line react-hooks/exhaustive-deps

  const start = async (full: boolean) => {
    setAsking(null);
    setError(null);
    setStarting(true);
    try {
      setJob((await bookkeeper.run(full)).run);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setStarting(false);
    }
  };

  if (!coverage && job.state === "idle") return null;
  const upToDate = !!coverage?.available && coverage.days_behind === 0;
  const running = job.state === "running";

  return (
    <div id="dash-bookkeeper" className="mt-6 scroll-mt-28 rounded-lg border-2 border-rule bg-check/40 p-5">
      <h3 className="font-display text-2xl font-extrabold text-ink [font-stretch:80%]">Run the Bookkeeper</h3>
      {coverage && <p className="mt-1 max-w-[62ch] text-[0.9375rem] text-carbon">{plan(coverage)}</p>}

      {running ? (
        <p className="mt-3 max-w-[62ch] text-[0.9375rem] font-bold text-ink" aria-live="polite">
          The Bookkeeper is running
          {job.full ? " from scratch" : ""} (started {at(job.started_at!)}, {since(job.started_at!, now)} ago). You can keep using Hermes: its
          findings come up above when it's done.
        </p>
      ) : (
        <>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={starting}
              onClick={() => (upToDate ? void start(false) : setAsking("run"))}
              className={`${button} border-ink bg-ink text-paper hover:bg-ink/90 disabled:opacity-60`}
            >
              {upToDate ? <IconRefresh aria-hidden="true" size={18} /> : <IconPlayerPlay aria-hidden="true" size={18} />}
              {starting ? "Starting…" : upToDate ? "Check for new transactions" : "Run the Bookkeeper"}
            </button>
            {coverage?.available && (
              <button
                type="button"
                disabled={starting}
                onClick={() => setAsking("full")}
                className={`${button} border-transparent text-ink underline underline-offset-4 hover:bg-check disabled:opacity-60`}
              >
                Re-check everything from scratch
              </button>
            )}
          </div>
          {asking && (
            <div role="group" aria-labelledby="bk-ask" className="mt-3 max-w-[62ch] rounded-md border-2 border-ink bg-paper p-4">
              <p id="bk-ask" className="text-[0.9375rem] text-carbon">
                {asking === "full" || !coverage?.available
                  ? "This starts a real run: the Bookkeeper reads a year of bank transactions from scratch with ASI:One, then its Verifier checks every finding. It can take up to an hour and uses ASI:One credits."
                  : "This starts a real run: the Bookkeeper re-checks the books with ASI:One, then its Verifier checks every finding. It can take up to 20 minutes and uses ASI:One credits."}{" "}
                Today's findings stay until it's done; then you review the new ones and sign again.
              </p>
              <div className="mt-3 flex flex-wrap gap-2">
                <button type="button" onClick={() => void start(asking === "full")} className={`${button} border-ink bg-ink text-paper hover:bg-ink/90`}>
                  Yes, run it
                </button>
                <button
                  type="button"
                  onClick={() => setAsking(null)}
                  className={`${button} border-transparent text-ink underline underline-offset-4 hover:bg-check`}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </>
      )}

      {!running && !starting && job.state !== "idle" && job.note && (
        <p
          className={`mt-3 flex max-w-[62ch] items-start gap-2 text-[0.9375rem] font-bold ${job.state === "failed" ? "text-serial" : "text-ink"}`}
          role={job.state === "failed" ? "alert" : undefined}
          aria-live="polite"
        >
          {job.state === "failed" ? (
            <IconAlertTriangle aria-hidden="true" size={20} className="mt-0.5 shrink-0" />
          ) : (
            <IconCheck aria-hidden="true" size={20} className="mt-0.5 shrink-0" />
          )}
          <span>
            {job.finished_at ? `${at(job.finished_at)}: ` : ""}
            {job.note}
          </span>
        </p>
      )}
      {error && (
        <p role="alert" className="mt-3 text-[0.9375rem] font-bold text-serial">
          {error}
        </p>
      )}
    </div>
  );
}
