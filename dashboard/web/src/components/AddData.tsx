import { useEffect, useRef, useState } from "react";
import { followRun, remember } from "../api";
import { GetStarted } from "./GetStarted";

type Phase = { state: "setup"; error?: string } | { state: "running"; text: string };

/**
 * Get started, in the Chat: connect the bank, then upload the records or use the sample, and start the check. The Chat
 * opens with it, and shows it again under a question that arrives while the data is missing (`holding`: that question is
 * answered once the data is in). onReady fires when the check's forecast is done; the rest of the check keeps running and
 * the dashboard picks it up.
 */
export function AddData({
  onReady,
  title = "Add your data",
  intro,
  holding = false,
}: {
  onReady: (restaurant: string) => void;
  title?: string;
  intro?: string;
  holding?: boolean;
}) {
  const [phase, setPhase] = useState<Phase>({ state: "setup" });
  const stop = useRef<(() => void) | null>(null);

  useEffect(() => () => stop.current?.(), []);

  const follow = (runId: string, guess: string) => {
    remember("boh-run", runId); // the dashboard shows this check, from the start, when the owner opens it
    setPhase({ state: "running", text: "Starting the check" });
    let restaurant = guess;
    let done = false;
    stop.current = followRun(
      runId,
      (e) => {
        if (done) return;
        if (e.type === "run") restaurant = e.restaurant;
        else if (e.type === "step" && e.state === "working") setPhase({ state: "running", text: e.text });
        else if (e.type === "step" && e.step === "forecast" && e.state === "done") {
          done = true;
          stop.current?.();
          onReady(restaurant);
        } else if (e.type === "error") {
          done = true;
          setPhase({ state: "setup", error: `The check stopped: ${e.text}. Nothing was changed. Try again.` });
        }
      },
      () => {
        if (!done) setPhase({ state: "setup", error: "Lost the connection to the check. Try again." });
      },
    );
  };

  if (phase.state === "running")
    return (
      <div className="mt-3 flex items-start gap-3 rounded-lg border-2 border-ink bg-paper p-4" role="status">
        <svg aria-hidden="true" viewBox="0 0 24 24" className="h-6 w-6 shrink-0 animate-spin text-ink [animation-duration:1.4s]" fill="none" strokeWidth="2.5">
          <circle cx="12" cy="12" r="9.5" stroke="var(--color-rule)" />
          <path d="M12 2.5a9.5 9.5 0 0 1 9.5 9.5" stroke="currentColor" strokeLinecap="round" />
        </svg>
        <div>
          <p className="font-bold text-ink">{phase.text}…</p>
          <p className="mt-1 text-sm text-pencil">
            {holding
              ? "Your question is saved; the answer follows as soon as your data is in."
              : "You can ask questions as soon as your data is in. The rest of the check carries on in the background."}
          </p>
        </div>
      </div>
    );

  return (
    <div className="mt-3">
      {phase.error && (
        <p role="alert" className="mb-3 text-[0.9375rem] font-bold text-serial">
          {phase.error}
        </p>
      )}
      <GetStarted
        title={title}
        intro={
          intro ??
          (holding
            ? "Your question is saved. Connect the bank and upload your books export, or try Hermes on the sample restaurant's books."
            : "Connect the bank and upload your books export, or try Hermes on the sample restaurant's books.")
        }
        onStarted={follow}
      />
    </div>
  );
}
