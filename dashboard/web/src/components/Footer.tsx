import { useState } from "react";
import { owner } from "../api";
import { LINKS } from "../links";
import { Logo } from "./Header";

const NOTES = [
  "Demo Bistro is a made-up restaurant, and its bank is Nessie test data, so no real money moves.",
  "Checks and the Chat only read your data. They can't change your books.",
  "The forecast plays out 1,000 possible futures. It's a guide, not financial advice.",
];

// On ink, like the header's bar, so links and focus rings are drawn in paper.
const LINK = "text-paper underline-offset-4 hover:underline focus-visible:outline-paper";

/** "Reset the demo": asks first, then clears every approval and the owner passphrase so the suggestions can be shown
 * again. The decisions on the findings stay. The page reloads to show the reset state. */
function ResetDemo() {
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const button = "min-h-11 rounded-md px-4 text-[0.9375rem] font-bold focus-visible:outline-paper";

  const reset = async () => {
    setBusy(true);
    setError(null);
    try {
      await owner.resetDemo();
      window.location.reload();
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  };

  if (!asking)
    return (
      <button type="button" onClick={() => setAsking(true)} className={`${button} mt-4 border-2 border-paper/60 text-paper hover:bg-white/10`}>
        Reset the demo
      </button>
    );
  return (
    <div role="group" aria-labelledby="reset-demo" className="mt-4 rounded-md border-2 border-paper/40 p-4">
      <p id="reset-demo" className="text-[0.9375rem] text-paper">
        This clears every approved change and the owner passphrase, so the suggestions come back. Your decisions on the findings stay: sign the review again
        and set a new passphrase.
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        <button type="button" onClick={reset} disabled={busy} className={`${button} bg-paper text-ink hover:bg-check disabled:opacity-60`}>
          {busy ? "Resetting…" : "Yes, reset the demo"}
        </button>
        <button type="button" onClick={() => setAsking(false)} disabled={busy} className={`${button} text-paper underline underline-offset-4 hover:bg-white/10`}>
          Cancel
        </button>
      </div>
      {error && (
        <p role="alert" className="mt-2 text-sm font-bold text-paper">
          {error}
        </p>
      )}
    </div>
  );
}

export function Footer() {
  return (
    <footer className="bg-ink text-paper">
      <div className="mx-auto grid max-w-6xl gap-10 px-4 py-14 sm:px-6 md:grid-cols-[minmax(0,1.3fr)_minmax(0,0.7fr)_minmax(0,1.3fr)] 2xl:max-w-7xl">
        <div>
          <a href="#/" className={`inline-flex min-h-11 items-center gap-2 font-display text-2xl font-extrabold no-underline [font-stretch:80%] ${LINK}`}>
            <Logo className="h-10 w-10" />
            Hermes
          </a>
          <p className="mt-3 max-w-[38ch] text-[0.9375rem] text-paper/80">
            Checks a restaurant's bank against its books, then plays out 1,000 futures to show when cash runs short.
          </p>
        </div>

        <nav aria-labelledby="footer-pages">
          <h2 id="footer-pages" className="font-bold">
            Pages
          </h2>
          <ul className="mt-2">
            {LINKS.map((l) => (
              <li key={l.route}>
                <a href={l.href} className={`inline-flex min-h-11 items-center text-[0.9375rem] no-underline ${LINK}`}>
                  {l.label}
                </a>
              </li>
            ))}
          </ul>
        </nav>

        <div>
          <h2 className="font-bold">About this demo</h2>
          <ul className="mt-4 space-y-3 text-[0.9375rem] text-paper/80">
            {NOTES.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
          <ResetDemo />
        </div>
      </div>

      <div className="border-t border-white/15">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-x-6 gap-y-2 px-4 py-5 text-sm text-paper/80 sm:px-6 2xl:max-w-7xl">
          <p>© 2026 Hermes</p>
          <p>
            Built at{" "}
            <a href="https://www.calhacks.io" target="_blank" rel="noreferrer" className={`font-bold underline ${LINK}`}>
              Cal Hacks 13.0<span className="sr-only"> (opens in a new tab)</span>
            </a>
            , October 2026
          </p>
        </div>
      </div>
    </footer>
  );
}
