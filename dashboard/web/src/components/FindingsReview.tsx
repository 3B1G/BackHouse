import { IconCheck, IconX } from "@tabler/icons-react";
import { useState } from "react";
import { owner, type OwnerReview, type ReviewFinding } from "../api";
import { mediumDate, moneyExact } from "../format";
import { OwnerDialog } from "./OwnerDialog";

const label = (cause: string | null) => {
  const text = (cause ?? "a problem").replace(/_/g, " ");
  return text[0].toUpperCase() + text.slice(1);
};

function FindingCard({ f, locked, busy, onDecide }: { f: ReviewFinding; locked: boolean; busy: boolean; onDecide: (d: "accept" | "dismiss") => void }) {
  const decision = f.proposal?.decision;
  const button = (d: "accept" | "dismiss", text: string) => (
    <button
      type="button"
      aria-pressed={decision === d}
      disabled={locked || busy}
      onClick={() => onDecide(d)}
      className={`inline-flex min-h-11 items-center gap-1.5 rounded-md border-2 px-3.5 text-sm font-bold transition-colors duration-150 disabled:cursor-not-allowed ${
        decision === d ? "border-ink bg-ink text-paper" : "border-ink bg-paper text-ink hover:bg-check disabled:opacity-60"
      }`}
    >
      {d === "accept" ? <IconCheck aria-hidden="true" size={18} /> : <IconX aria-hidden="true" size={18} />}
      {text}
    </button>
  );
  return (
    <li className="rounded-md border-2 border-rule bg-paper px-5 py-4">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h3 className="font-display text-xl font-extrabold text-ink [font-stretch:80%]">
          {label(f.root_cause)}
          {f.counterparty ? <span className="font-sans text-base font-bold text-carbon [font-stretch:100%]">, {f.counterparty}</span> : null}
        </h3>
        <p className="font-display text-xl font-extrabold text-serial [font-stretch:80%]">{moneyExact(f.impact_cents)}</p>
      </div>
      <p className="mt-0.5 text-sm text-pencil">
        {f.first ? (f.last && f.last !== f.first ? `${mediumDate(f.first)} to ${mediumDate(f.last)}` : mediumDate(f.first)) : null}
        {f.verifier === "agreed" ? `${f.first ? ". " : ""}The Verifier found it too.` : ""}
      </p>
      {f.explanation && <p className="mt-2 text-[0.9375rem] text-carbon">{f.explanation}</p>}
      {f.suggested_action && <p className="mt-1.5 text-[0.9375rem] text-pencil">What to do: {f.suggested_action}</p>}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        {button("accept", "Accept")}
        {button("dismiss", "Dismiss")}
        <span className="text-sm text-pencil">
          {decision === "accept" ? "Counts in the forecast" : decision === "dismiss" ? "Left out of the forecast" : decision === "revise" ? "Sent back to the Bookkeeper" : "Not decided yet"}
        </span>
      </div>
    </li>
  );
}

/** "Problems in your books": the Bookkeeper's verified findings for the owner to accept or dismiss, then sign. Signing
 * the review is what lets the orchestrator forecast with them and suggest changes. */
export function FindingsReview({ review, onReview, onSigned }: { review: OwnerReview; onReview: (r: OwnerReview) => void; onSigned: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [signing, setSigning] = useState(false);
  const n = review.findings.length;
  const decided = review.findings.filter((f) => f.proposal).length;
  const agreed = review.batch?.finding_ids.length ?? 0;
  const locked = review.committed;

  const act = async (work: () => Promise<OwnerReview>) => {
    setBusy(true);
    setError(null);
    try {
      onReview(await work());
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const sign = async (passphrase: string) => {
    if (!review.owner_key) onReview(await owner.setup(passphrase)); // the key exists from here on, even if signing fails
    const r = await owner.commit(passphrase);
    setSigning(false);
    onReview(r);
    onSigned();
  };

  return (
    <div className="mt-3">
      <p className="max-w-[62ch] text-[1.0625rem] text-carbon">
        {locked
          ? `You signed this review. The forecast counts the ${review.findings.filter((f) => f.proposal?.decision === "accept").length} findings you accepted, and the suggested changes are open.`
          : `The Bookkeeper found ${n} problem${n === 1 ? "" : "s"}, and its Verifier checked each one. Accept the ones that are right and dismiss the rest: the forecast counts only what you accept. Then sign your review to open the suggested changes.`}
      </p>
      {!locked && agreed > 0 && (
        <button
          type="button"
          disabled={busy}
          onClick={() => act(owner.acceptVerified)}
          className="mt-3 inline-flex min-h-11 items-center gap-1.5 rounded-md border-2 border-ink bg-check px-4 text-[0.9375rem] font-bold text-ink hover:bg-check-deep disabled:opacity-60"
        >
          <IconCheck aria-hidden="true" size={18} />
          Accept all {agreed} the Verifier agreed with
        </button>
      )}
      <ol className="mt-4 space-y-3">
        {review.findings.map((f) => (
          <FindingCard key={f.finding_id} f={f} locked={locked} busy={busy} onDecide={(d) => act(() => owner.decide(f.finding_id, d))} />
        ))}
      </ol>
      {error && (
        <p role="alert" className="mt-3 text-[0.9375rem] font-bold text-serial">
          {error}
        </p>
      )}
      {!locked && (
        <div className="mt-4 flex flex-wrap items-center gap-3 rounded-md border-2 border-dashed border-rule p-4">
          <p className="text-[0.9375rem] text-carbon" aria-live="polite">
            {decided} of {n} decided
          </p>
          <button
            type="button"
            disabled={decided < n || busy}
            onClick={() => setSigning(true)}
            className="ml-auto min-h-11 rounded-md bg-ink px-5 text-[0.9375rem] font-bold text-paper hover:bg-ink/90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Sign my review
          </button>
        </div>
      )}
      <OwnerDialog
        open={signing}
        title="Sign your review"
        what={`${decided} decisions on the Bookkeeper's findings. Nothing in your books changes; the forecast uses what you accepted.`}
        action="Sign my review"
        setup={!review.owner_key}
        onClose={() => setSigning(false)}
        onSign={sign}
      />
    </div>
  );
}
