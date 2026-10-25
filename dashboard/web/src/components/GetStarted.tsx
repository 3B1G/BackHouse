import { useId, useState } from "react";
import { api, type Account } from "../api";

type Records =
  | { state: "idle" }
  | { state: "checking"; name: string }
  | { state: "ready"; source: "sample" | "upload"; name: string; uploadId?: string; detail: string }
  | { state: "error"; message: string };

function Done({ children }: { children: React.ReactNode }) {
  return (
    <p className="mt-3 flex items-start gap-2 text-[0.9375rem] text-ink">
      <svg aria-hidden="true" viewBox="0 0 20 20" className="mt-0.5 h-5 w-5 shrink-0" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="10" cy="10" r="8" />
        <path d="M6.5 10.5l2.3 2.3 4.7-5" />
      </svg>
      <span>{children}</span>
    </p>
  );
}

function Problem({ children }: { children: React.ReactNode }) {
  return (
    <p role="alert" className="mt-3 text-[0.9375rem] font-bold text-serial">
      {children}
    </p>
  );
}

const button =
  "inline-flex min-h-11 items-center justify-center rounded-md border-2 px-4 text-[0.9375rem] font-bold transition-colors duration-150 disabled:opacity-50";

export function GetStarted({ onStarted }: { onStarted: (runId: string, restaurant: string) => void }) {
  const fileId = useId();
  const helpId = useId();
  const [bank, setBank] = useState<{ state: "idle" | "connecting" | "error" | "connected"; accounts?: Account[]; message?: string }>({
    state: "idle",
  });
  const [records, setRecords] = useState<Records>({ state: "idle" });
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  const connect = async () => {
    setBank({ state: "connecting" });
    try {
      const { accounts } = await api.connectBank();
      setBank({ state: "connected", accounts });
    } catch (e) {
      setBank({ state: "error", message: (e as Error).message });
    }
  };

  const upload = async (file: File | undefined) => {
    if (!file) return;
    setRecords({ state: "checking", name: file.name });
    try {
      const u = await api.uploadRecords(file);
      setRecords({ state: "ready", source: "upload", name: u.name, uploadId: u.upload_id, detail: `${u.invoices} invoices` });
    } catch (e) {
      setRecords({ state: "error", message: (e as Error).message });
    }
  };

  const ready = bank.state === "connected" && records.state === "ready";

  const start = async () => {
    if (records.state !== "ready") return;
    setStarting(true);
    setStartError(null);
    try {
      const { run_id } = await api.startRun({ source: records.source, upload_id: records.uploadId, name: records.name });
      onStarted(run_id, "Demo Bistro, Chestnut St");
    } catch (e) {
      setStartError((e as Error).message);
      setStarting(false);
    }
  };

  return (
    <section aria-labelledby="start-heading" className="rounded-lg border-2 border-ink bg-paper p-5">
      <h2 id="start-heading" className="font-display text-2xl font-extrabold text-ink [font-stretch:80%]">
        Get started
      </h2>
      <ol className="mt-4 space-y-5">
        <li>
          <h3 className="text-[1.0625rem] font-bold">1. Connect the bank</h3>
          <p className="mt-1 text-[0.9375rem] text-pencil">Read-only, through Nessie, Capital One's banking API. Nothing moves money.</p>
          {bank.state === "connected" ? (
            <Done>Connected: {bank.accounts?.map((a) => a.name).join(" and ")}</Done>
          ) : (
            <button type="button" onClick={connect} disabled={bank.state === "connecting"} className={`${button} mt-3 border-ink bg-ink text-white hover:bg-[#173b29]`}>
              {bank.state === "connecting" ? "Connecting…" : "Connect"}
            </button>
          )}
          {bank.state === "error" && <Problem>{bank.message}</Problem>}
        </li>
        <li>
          <h3 className="text-[1.0625rem] font-bold">2. Add your records</h3>
          <p className="mt-1 text-[0.9375rem] text-pencil">Invoices, bills, payroll and sales, from your books export (.db).</p>
          {records.state === "ready" ? (
            <Done>
              {records.name}: {records.detail}
            </Done>
          ) : (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <label htmlFor={fileId} className={`${button} cursor-pointer border-ink bg-paper text-ink hover:bg-check`}>
                {records.state === "checking" ? `Checking ${records.name}…` : "Choose file"}
              </label>
              <input id={fileId} type="file" accept=".db,.sqlite" className="sr-only" onChange={(e) => upload(e.target.files?.[0])} />
              <button
                type="button"
                onClick={() => setRecords({ state: "ready", source: "sample", name: "Demo Bistro sample", detail: "a year of books" })}
                className={`${button} border-transparent text-ink underline underline-offset-4 hover:bg-check`}
              >
                Use the sample
              </button>
            </div>
          )}
          {records.state === "error" && <Problem>{records.message}</Problem>}
        </li>
      </ol>
      <div className="mt-6 border-t border-rule pt-5">
        <button
          type="button"
          onClick={start}
          disabled={!ready || starting}
          aria-describedby={ready ? undefined : helpId}
          className={`${button} min-h-12 w-full border-serial bg-serial px-6 text-base text-white hover:bg-[#931f18] disabled:border-rule disabled:bg-check disabled:text-pencil disabled:opacity-100`}
        >
          {starting ? "Starting…" : "Start the check"}
        </button>
        {!ready && (
          <p id={helpId} className="mt-2 text-sm text-pencil">
            Connect the bank and add your records first.
          </p>
        )}
        {startError && <Problem>{startError}</Problem>}
      </div>
    </section>
  );
}
