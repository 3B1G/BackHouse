import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { Decision, DecideBody, Review as ReviewData, ReviewFinding, Stage } from "../api";

/** What each stage means for the owner, and what comes after it. */
const STAGES: { id: Stage; name: string; next: string }[] = [
  { id: "Audit", name: "Audit", next: "The Bookkeeper checks the books. Its findings come here for review." },
  { id: "Review", name: "Review", next: "Accept, dismiss or revise each finding. Then commit the review at your terminal." },
  { id: "Commit", name: "Commit", next: "Every finding has a decision. Commit the review at your terminal to unlock the forecast." },
  { id: "Forecast", name: "Forecast", next: "The review is committed. The Forecaster runs on the fixes you accepted." },
  { id: "Suggest", name: "Suggest", next: "The forecast is in. Suggested fixes wait for you to approve them at your terminal." },
];

const VERIFIER: Record<string, string> = {
  agreed: "The Verifier agrees",
  replaced: "The Verifier replaced the Bookkeeper's version",
  verifier_only: "Found by the Verifier only",
  unverified: "Not verified",
};

const DECIDED: Record<Decision, string> = { accept: "Accepted", dismiss: "Dismissed", revise: "Sent back for revision" };

function StageBanner({ review }: { review: ReviewData }) {
  const stage = review.stage;
  const at = stage ? STAGES.findIndex((s) => s.id === stage.stage) : 0;
  const here = STAGES[Math.max(0, at)];
  return (
    <section aria-labelledby="stage-heading" className="rounded-lg border-2 border-rule bg-paper p-5">
      <ol className="flex flex-wrap gap-2" aria-label="Stages">
        {STAGES.map((s, i) => (
          <li
            key={s.id}
            aria-current={i === at ? "step" : undefined}
            className={`rounded-md px-3 py-1 text-sm font-bold ${
              i === at ? "bg-ink text-white" : i < at ? "bg-check text-ink" : "border border-dashed border-rule text-pencil"
            }`}
          >
            {s.name}
          </li>
        ))}
      </ol>
      <h2 id="stage-heading" className="mt-4 font-display text-2xl font-extrabold text-ink [font-stretch:80%]">
        Stage: {here.name}
      </h2>
      <p className="mt-1 max-w-[62ch] text-[1.0625rem] text-carbon">{here.next}</p>
      {stage?.why && <p className="mt-1 max-w-[62ch] text-[0.9375rem] text-pencil">{stage.why}</p>}
      {stage && stage.open_findings > 0 && (
        <p className="mt-2 text-[0.9375rem] font-bold text-ink">
          {stage.decided} of {stage.open_findings} findings decided
        </p>
      )}
    </section>
  );
}

/** How the owner commits: at their own terminal, with the passphrase. This page never commits. */
function CommitSteps({ review }: { review: ReviewData }) {
  const command = review.stage?.approve_command;
  return (
    <section aria-labelledby="commit-heading" className="rounded-lg border-2 border-dashed border-rule p-5">
      <h2 id="commit-heading" className="font-display text-xl font-extrabold text-ink [font-stretch:80%]">
        Commit the review at your terminal
      </h2>
      <p className="mt-1 max-w-[62ch] text-[0.9375rem] text-carbon">{review.note}</p>
      <ol className="mt-3 list-decimal space-y-2 pl-5 text-[0.9375rem] text-carbon">
        <li>
          In <code className="rounded bg-check px-1.5 py-0.5">nessie_lab</code>, run{" "}
          <code className="rounded bg-check px-1.5 py-0.5">orchestrator review prepare</code>
        </li>
        <li>
          Then run{" "}
          {command ? (
            <code className="break-all rounded bg-check px-1.5 py-0.5">{command}</code>
          ) : (
            <>
              the <code className="rounded bg-check px-1.5 py-0.5">changes approve …</code> command it prints
            </>
          )}
          , and type your owner passphrase when it asks.
        </li>
      </ol>
    </section>
  );
}

function FindingCard({
  f,
  busy,
  onDecide,
}: {
  f: ReviewFinding;
  busy: boolean;
  onDecide: (decision: Decision, note?: string) => void;
}) {
  const [note, setNote] = useState("");
  const noteId = `note-${f.finding_id}`;
  const button = "min-h-11 rounded-md px-4 text-[0.9375rem] font-bold transition-colors duration-150 disabled:opacity-50";
  return (
    <li className="rounded-lg border-2 border-rule bg-paper p-5">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h3 className="font-bold text-ink">{f.finding_id}</h3>
        {f.verdict && (
          <span className={`text-sm font-bold ${f.verdict === "error" ? "text-serial" : "text-pencil"}`}>
            {f.verdict === "error" ? "Error" : f.verdict === "needs_owner" ? "Needs you" : f.verdict}
          </span>
        )}
        {f.impact && <span className="ml-auto font-bold text-carbon">{f.impact.usd}</span>}
      </div>
      {f.root_cause && <p className="mt-2 max-w-[70ch] text-[1.0625rem] text-carbon">{f.root_cause}</p>}
      <dl className="mt-3 grid gap-x-6 gap-y-1 text-[0.9375rem] sm:grid-cols-2">
        {f.counterparty && (
          <div>
            <dt className="inline text-pencil">Who: </dt>
            <dd className="inline text-carbon">{f.counterparty}</dd>
          </div>
        )}
        <div>
          <dt className="inline text-pencil">Verifier: </dt>
          <dd className="inline text-carbon">{f.verifier ? (VERIFIER[f.verifier] ?? f.verifier) : "Not verified"}</dd>
        </div>
        <div className="sm:col-span-2">
          <dt className="inline text-pencil">Your decision: </dt>
          <dd className={`inline ${f.proposal ? "font-bold text-ink" : "text-carbon"}`}>
            {f.proposal ? DECIDED[f.proposal.decision] ?? f.proposal.decision : "Not decided yet"}
            {f.proposal?.note ? ` (${f.proposal.note})` : ""}
          </dd>
        </div>
      </dl>
      <div className="mt-4 border-t border-dashed border-rule pt-4">
        <label htmlFor={noteId} className="text-sm font-bold text-carbon">
          Note (needed to revise)
        </label>
        <input
          id={noteId}
          value={note}
          maxLength={500}
          onChange={(e) => setNote(e.target.value)}
          className="mt-1 block min-h-11 w-full max-w-xl rounded-md border-2 border-rule bg-white px-3 text-[0.9375rem]"
        />
        <div className="mt-3 flex flex-wrap gap-2">
          <button type="button" disabled={busy} onClick={() => onDecide("accept", note)} className={`${button} bg-ink text-white hover:bg-[#173b29]`}>
            Accept
          </button>
          <button type="button" disabled={busy} onClick={() => onDecide("dismiss", note)} className={`${button} border-2 border-ink text-ink hover:bg-check`}>
            Dismiss
          </button>
          <button
            type="button"
            disabled={busy || !note.trim()}
            onClick={() => onDecide("revise", note)}
            className={`${button} border-2 border-ink text-ink hover:bg-check`}
          >
            Revise
          </button>
        </div>
      </div>
    </li>
  );
}

/** The owner's review of the Bookkeeper's findings, through the orchestrator. Decisions are proposals; commit is at the terminal. */
export function Review() {
  const [review, setReview] = useState<ReviewData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await api.review();
      setReview(r);
      setError(r.ok ? null : (r.error ?? "The orchestrator couldn't read the review."));
    } catch (e) {
      setReview(null);
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const decide = async (body: DecideBody) => {
    setBusy(true);
    try {
      const r = await api.decide(body);
      setError(r.ok ? null : (r.error ?? "The orchestrator didn't record that."));
    } catch (e) {
      setError((e as Error).message);
    }
    await load();
    setBusy(false);
  };

  const findings = review?.ok ? review.findings : [];
  const batch = review?.batch?.finding_ids?.length ? review.batch : null;
  const stage = review?.stage?.stage;

  return (
    <div className="mx-auto max-w-6xl px-4 pb-16 pt-10 sm:px-6 2xl:max-w-7xl">
      <h1 className="font-display text-[clamp(2.25rem,4vw,3.5rem)] font-extrabold leading-none text-ink [font-stretch:66%]">Review</h1>
      <p className="mt-3 max-w-[62ch] text-lg text-carbon">
        Decide what to do with each thing the Bookkeeper found. Nothing here changes your books: you commit the review yourself, at your
        terminal.
      </p>

      {error && (
        <div className="mt-6 rounded-lg border-2 border-serial-mid bg-serial-soft p-5">
          <p role="alert" className="text-[0.9375rem] font-bold text-serial">
            {error}
          </p>
          <button type="button" onClick={() => void load()} className="mt-3 min-h-11 rounded-md border-2 border-ink px-4 text-[0.9375rem] font-bold text-ink hover:bg-check">
            Try again
          </button>
        </div>
      )}

      {review?.ok && (
        <div className="mt-8 space-y-8">
          <StageBanner review={review} />

          {findings.length === 0 ? (
            <section className="rounded-lg border-2 border-dashed border-rule p-6">
              <p className="max-w-[56ch] text-[1.0625rem] text-carbon">
                {!stage || stage === "Audit"
                  ? "There's no Bookkeeper report to review yet. Its findings appear here once it has checked the books."
                  : review.committed
                    ? "The review is committed. There's nothing left to decide."
                    : "There are no open findings to decide."}
              </p>
            </section>
          ) : (
            <section aria-labelledby="findings-heading">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <h2 id="findings-heading" className="font-display text-3xl font-extrabold text-ink [font-stretch:80%]">
                  {findings.length} {findings.length === 1 ? "finding" : "findings"} to review
                </h2>
                {batch && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void decide({ accept_verified_errors: true })}
                    className="min-h-11 rounded-md bg-ink px-5 font-bold text-white transition-colors duration-150 hover:bg-[#173b29] disabled:opacity-50"
                  >
                    Accept all verified errors ({batch.finding_ids?.length})
                  </button>
                )}
              </div>
              <ul aria-busy={busy} className="mt-4 space-y-4">
                {findings.map((f) => (
                  <FindingCard
                    key={f.finding_id}
                    f={f}
                    busy={busy}
                    onDecide={(decision, note) =>
                      void decide({ finding: f.finding_id, decision, ...(note?.trim() ? { note: note.trim() } : {}) })
                    }
                  />
                ))}
              </ul>
            </section>
          )}

          {stage && stage !== "Audit" && !review.committed && <CommitSteps review={review} />}
        </div>
      )}
    </div>
  );
}
