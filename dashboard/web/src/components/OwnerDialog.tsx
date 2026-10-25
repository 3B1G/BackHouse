import { useEffect, useRef, useState, type FormEvent } from "react";

/** Asks for the owner passphrase, which signs what `what` describes. The first time there is no owner key yet, so it
 * sets one up instead: the passphrase twice, at least 10 characters. The passphrase goes to the local API once, for
 * this one signature, and is cleared from the page right after. */
export function OwnerDialog({
  open,
  title,
  what,
  action,
  setup,
  onClose,
  onSign,
}: {
  open: boolean;
  title: string;
  what: string;
  action: string;
  setup: boolean;
  onClose: () => void;
  onSign: (passphrase: string) => Promise<void>;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [pass, setPass] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) {
      setPass("");
      setAgain("");
      setError(null);
      d.showModal();
    } else if (!open && d.open) d.close();
  }, [open]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (setup && pass.length < 10) return setError("Use at least 10 characters.");
    if (setup && pass !== again) return setError("The two passphrases don't match.");
    setBusy(true);
    setError(null);
    try {
      await onSign(pass);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
      setPass("");
      setAgain("");
    }
  };

  const field = "mt-1 block min-h-11 w-full rounded-md border-2 border-rule bg-paper px-3 text-base text-carbon focus-visible:border-ink";
  return (
    <dialog
      ref={ref}
      onClose={onClose}
      aria-labelledby="owner-title"
      className="m-auto w-[min(30rem,calc(100vw-2rem))] rounded-lg border-2 border-rule bg-paper p-0 text-carbon shadow-[0_24px_48px_-20px_rgba(31,77,54,0.55)] backdrop:bg-carbon/45"
    >
      <form onSubmit={submit} className="stub border-t-[3px] border-rule p-6">
        <h2 id="owner-title" className="font-display text-2xl font-extrabold text-ink [font-stretch:80%]">
          {title}
        </h2>
        <p className="mt-2 rounded-md bg-check px-3 py-2 text-[0.9375rem] font-bold text-ink">{what}</p>
        {setup ? (
          <p className="mt-3 text-[0.9375rem]">
            First, choose your owner passphrase. It signs every change Hermes makes for you, and Hermes never stores it. Write it down: no one can
            recover it.
          </p>
        ) : (
          <p className="mt-3 text-[0.9375rem]">Your owner passphrase signs this. Nothing changes without it.</p>
        )}
        <label className="mt-4 block text-sm font-bold text-ink">
          {setup ? "New owner passphrase" : "Owner passphrase"}
          <input
            type="password"
            autoFocus
            required
            autoComplete={setup ? "new-password" : "current-password"}
            value={pass}
            onChange={(e) => setPass(e.target.value)}
            className={field}
          />
        </label>
        {setup && (
          <label className="mt-3 block text-sm font-bold text-ink">
            Type it again
            <input type="password" required autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} className={field} />
          </label>
        )}
        {error && (
          <p role="alert" className="mt-3 text-[0.9375rem] font-bold text-serial">
            {error}
          </p>
        )}
        <div className="mt-5 flex flex-wrap justify-end gap-2">
          <button type="button" onClick={onClose} className="min-h-11 rounded-md px-4 text-[0.9375rem] font-bold text-ink underline underline-offset-4 hover:bg-check">
            Cancel
          </button>
          <button
            type="submit"
            disabled={busy}
            className="min-h-11 rounded-md bg-ink px-5 text-[0.9375rem] font-bold text-paper transition-colors duration-150 hover:bg-ink/90 disabled:opacity-60"
          >
            {busy ? "Signing…" : action}
          </button>
        </div>
      </form>
    </dialog>
  );
}
