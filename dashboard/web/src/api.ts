// Shapes the FastAPI backend (dashboard/api) returns, and the calls that fetch them.

/** A payday ahead: the chance payroll clears, and the lowest balance until the next payday (10th, 50th, 90th percentile). */
export type Payday = { date: string; clears: number; p10: number; p50: number; p90: number };
/** A month ahead: how much cash grows (or shrinks, below zero) across the futures. */
export type MonthAhead = { month: string; p10: number; p50: number; p90: number };
export type MonthPast = { month: string; in_cents: number; out_cents: number; net_cents: number; through?: string };
export type SpendRow = { label: string; cents: number; parts: { label: string; cents: number }[] };

export type Forecast = {
  as_of: string;
  cash_today_cents: number;
  floor_cents: number;
  runout: { bad: string | null; likely: string | null; good: string | null };
  share_running_out: number;
  below_floor_likely: string | null;
  paydays: Payday[];
  base_paydays: Payday[] | null;
  base_likely: string | null;
  months: MonthAhead[];
  base_months: MonthAhead[] | null;
  history: {
    paydays: { date: string; low_cents: number }[];
    months: MonthPast[];
    spending: { from: string; to: string; in_cents: number; out_cents: number; rows: SpendRow[] };
  };
  window_end: string;
  vs_base: { avg_days: number | null; likely_days: number | null; floor_days: number | null } | null;
  what_if: string[];
  assumptions: string[];
  runtime_ms: number;
};

/** One what-if the owner can switch on, with how far it moves the day cash runs out on its own. */
export type Scenario = {
  id: string;
  group: "risk" | "change";
  title: string;
  detail: string;
  available: boolean;
  effect_days: number | null;
};

export type StepId = "bank" | "records" | "matcher" | "bookkeeper" | "forecast";
export type StepState = "working" | "done" | "waiting";
export type StepEvent = { type: "step"; step: StepId; state: StepState; text: string; t: number; forecast?: Forecast };

export type Finding = {
  finding_id: string;
  issue_type: string;
  verdict: "error" | "ok" | "needs_owner";
  root_cause: string;
  counterparty: string | null;
  first_date: string | null;
  last_date: string | null;
  bank_ids: string[];
  books_refs: string[];
  amount_at_issue_cents: number;
  monthly_impact_cents: number | null;
  explanation: string;
  suggested_action: string | null;
  confidence: number;
};

export type RunEvent =
  | { type: "run"; restaurant: string; t: number }
  | StepEvent
  | { type: "finding"; finding: Finding; t: number }
  | { type: "done"; t: number }
  | { type: "error"; text: string; t: number };

/** The orchestrator's stages, in order. Each unlocks the next; the review is committed only at the owner's terminal. */
export type Stage = "Audit" | "Review" | "Commit" | "Forecast" | "Suggest";
export type StageInfo = {
  stage: Stage;
  why: string;
  review_id: string | null;
  as_of: string | null;
  report: string | null;
  open_findings: number;
  decided: number;
  digest: string | null;
  approve_command: string | null;
  suggestion_file: string | null;
  order: Stage[];
};

export type Decision = "accept" | "dismiss" | "revise";
/** Money from the orchestrator: cents and a ready-made "$1,234.56". */
export type OrchMoney = { cents: number; usd: string };

export type ReviewFinding = {
  finding_id: string;
  verdict: string | null;
  root_cause: string | null;
  counterparty: string | null;
  impact: OrchMoney | null;
  verifier: string | null;
  proposal: { decision: Decision; note: string | null; forced: boolean | null; proposed_by: string; decided_at: string } | null;
};

/** The orchestrator answers problems as ok: false with an error (HTTP 200), so check ok before the rest. */
export type Review = {
  ok: boolean;
  error: string | null;
  stage: StageInfo | null;
  committed: boolean;
  findings: ReviewFinding[];
  batch: { finding_ids?: string[]; line?: string | null };
  note: string;
};

export type DecideResult = { ok: boolean; error: string | null; proposed: Record<string, unknown>[]; stage: StageInfo | null; committed: boolean };
export type DecideBody = { finding: string; decision: Decision; note?: string } | { accept_verified_errors: true };

export type Account = { name: string; type: string };
export type Upload = { upload_id: string; name: string; size: number; invoices: number };

async function read<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let message = `Something went wrong (error ${res.status}). Try again.`;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") message = body.detail;
    } catch {
      // keep the generic message
    }
    throw new Error(message);
  }
  return res.json() as Promise<T>;
}

export const api = {
  connectBank: () => fetch("/api/bank/connect", { method: "POST" }).then((r) => read<{ accounts: Account[] }>(r)),
  uploadRecords: (file: File) => {
    const body = new FormData();
    body.append("file", file);
    return fetch("/api/records", { method: "POST", body }).then((r) => read<Upload>(r));
  },
  startRun: (body: { source: "sample" | "upload"; upload_id?: string; name?: string }) =>
    fetch("/api/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then((r) =>
      read<{ run_id: string }>(r),
    ),
  forecast: (whatIfs: string[]) =>
    fetch(`/api/forecast?w=${encodeURIComponent(whatIfs.join(","))}`).then((r) => read<Forecast>(r)),
  scenarios: () => fetch("/api/scenarios").then((r) => read<{ scenarios: Scenario[] }>(r)),
  /** The orchestrator's review, through the dashboard server (which holds its token). */
  review: () => fetch("/api/orch/review").then((r) => read<Review>(r)),
  /** Records a proposed decision. It never commits: the owner does that at their terminal. */
  decide: (body: DecideBody) =>
    fetch("/api/orch/review/decide", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then(
      (r) => read<DecideResult>(r),
    ),
};

export type ChatTurn = { role: "user" | "assistant"; content: string };
export type ChatEvent =
  | { type: "tool"; text: string }
  | { type: "answer"; text: string; sources: string[]; off_topic?: boolean }
  | { type: "error"; text: string };

/** Asks the chat a question with the conversation so far; streams what it looks at, then the answer. */
export async function askChat(messages: ChatTurn[], onEvent: (e: ChatEvent) => void) {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages }),
  });
  if (!res.ok || !res.body) throw new Error(`The chat didn't answer (error ${res.status}). Try again.`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      for (const line of chunk.split("\n")) if (line.startsWith("data: ")) onEvent(JSON.parse(line.slice(6)) as ChatEvent);
    }
  }
}

/** Streams a run's events; calls onLost if the stream breaks before the run says it's done. */
export function followRun(runId: string, onEvent: (e: RunEvent) => void, onLost: () => void): () => void {
  const source = new EventSource(`/api/runs/${runId}/events`);
  let finished = false;
  source.onmessage = (m) => {
    const event = JSON.parse(m.data) as RunEvent;
    if (event.type === "done" || event.type === "error") finished = true;
    onEvent(event);
  };
  source.onerror = () => {
    source.close();
    if (!finished) onLost();
  };
  return () => source.close();
}

export function remember(key: string, value: string | null) {
  try {
    if (value === null) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, value);
  } catch {
    // private windows can refuse storage; the page still works without it
  }
}

export function recall(key: string): string | null {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

/** A preference that outlives the tab (which chart the owner likes to see). */
export function prefer(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // storage can be blocked; the default view still works
  }
}

export function preferred(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
