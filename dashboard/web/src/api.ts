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

export type StepId = "bank" | "records" | "matcher" | "bookkeeper" | "forecast" | "market" | "orchestrator";
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

/** One change the orchestrator suggests: dry-run by Changes and saved for the owner to approve, decline or ask about. */
export type Fix = {
  id: string;
  action: string;
  title: string;
  dish: string | null;
  old_price_cents: number | null;
  new_price_cents: number | null;
  market_median_cents: number | null;
  price_cents: number | null;
  food_cost_pct: number | null;
  starts: string | null;
  runout_before: string | null;
  runout_after: string | null;
  days_later: number | null;
  monthly_cents: number | null;
  approve: string;
  file: string;
};

/** One dish against nearby restaurants' own menus, from Market scout. gap_pct: 0.08 is 8% above the middle price. */
export type PricePosition = {
  our_item: string;
  our_price_cents: number;
  competitor_median_cents: number | null;
  competitor_low_cents: number | null;
  competitor_high_cents: number | null;
  n_prices: number;
  position: "below_market" | "at_par" | "above_market" | "insufficient_data";
  gap_pct: number | null;
};

/** The orchestrator's brief at the end of a check. */
export type Plan = {
  stage?: { stage: string | null; why: string | null } | null;
  as_of: string | null;
  cash_cents: number | null;
  bills: { before: string; count: number; total_cents: number } | null;
  market: { offline: boolean; menu_as_of: string; positions: PricePosition[] } | null;
  fixes: Fix[];
  /** Approved changes that haven't started yet: when nothing is left to suggest, these are why. */
  upcoming?: { dish: string; kind: string; new_price_cents: number | null; old_price_cents: number | null; available: number | boolean | null; starts: string }[];
  brief: string;
};

export type RunEvent =
  | { type: "run"; restaurant: string; t: number; started_at?: number }
  | StepEvent
  | { type: "finding"; finding: Finding; t: number }
  | { type: "plan"; plan: Plan; t: number }
  | { type: "done"; t: number }
  | { type: "error"; text: string; t: number };

export type Account = { name: string; type: string };
export type Upload = { upload_id: string; name: string; size: number; invoices: number };

/** One of the Bookkeeper's findings in the owner's review, with the owner's decision so far. */
export type ReviewFinding = {
  finding_id: string;
  verdict: string | null;
  root_cause: string | null;
  counterparty: string | null;
  impact_cents: number;
  verifier: string | null;
  explanation: string | null;
  suggested_action: string | null;
  first: string | null;
  last: string | null;
  proposal: { decision: "accept" | "dismiss" | "revise"; note: string | null } | null;
};

/** The owner's review: where the orchestrator's stages stand, each open finding, and whether the owner key exists. */
export type OwnerReview = {
  stage: { stage: string; why: string; decided: number; open_findings: number; report: string | null } & Record<string, unknown>;
  committed: boolean;
  findings: ReviewFinding[];
  batch?: { finding_ids: string[] };
  owner_key: boolean;
  fingerprint: string | null;
  signed?: string;
};

export type Approved = { line: string; applied: { applied: boolean; error: string | null } | null };

const post = <T>(url: string, body?: unknown) =>
  fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}) }).then((r) => read<T>(r));

/** The owner's buttons. Committing and approving sign with the owner key, so they need the owner passphrase. */
export const owner = {
  review: () => fetch("/api/owner").then((r) => read<OwnerReview>(r)),
  decide: (finding: string, decision: "accept" | "dismiss") => post<OwnerReview>("/api/owner/decide", { finding, decision }),
  acceptVerified: () => post<OwnerReview>("/api/owner/accept-verified"),
  setup: (passphrase: string) => post<OwnerReview>("/api/owner/setup", { passphrase }),
  commit: (passphrase: string) => post<OwnerReview>("/api/owner/commit", { passphrase }),
  approve: (file: string, passphrase: string) => post<Approved>("/api/owner/approve", { file, passphrase }),
  /** Demo only: clears every approval and the owner passphrase (the price database is rebuilt); the decisions on the
   * findings stay, so signing the review again brings the suggestions back. */
  resetDemo: () => post<OwnerReview>("/api/demo/reset", { confirm: "reset the demo" }),
  /** The owner's own price: forecast and dry-run, returned as a suggestion to approve. */
  propose: (dish: string, new_price_cents: number) => post<{ fix: Fix }>("/api/owner/propose", { dish, new_price_cents }),
  decline: (fx: Fix) => post<{ ok: boolean }>("/api/owner/decline", { id: fx.id, action: fx.action, dish: fx.dish, new_price_cents: fx.new_price_cents, title: fx.title }),
  plan: () => post<Plan>("/api/owner/plan"),
};

/** How far the Bookkeeper has checked the bank (the orchestrator's bookkeeper_status); null when it can't say. */
export type BookkeeperCoverage = {
  available: boolean;
  covered_through: string | null;
  latest_bank_date: string | null;
  days_behind: number | null;
  open_findings: number | null;
};

/** The owner's run of the Bookkeeper from the Chat's card (the orchestrator's update). `started_at` and `finished_at`
 * are Unix seconds. */
export type BookkeeperJob = {
  state: "idle" | "running" | "done" | "failed";
  started_at?: number;
  finished_at?: number | null;
  result?: "up_to_date" | "new_report" | "failed" | null;
  note?: string | null;
  last?: string | null;
};

export const bookkeeper = {
  status: () => fetch("/api/bookkeeper").then((r) => read<{ run: BookkeeperJob; coverage: BookkeeperCoverage | null }>(r)),
  /** The orchestrator's update: done at once and free when the Bookkeeper has checked every bank day. */
  run: () => post<{ run: BookkeeperJob }>("/api/bookkeeper/run", { confirm: "run the bookkeeper" }),
};

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
  /** Whether the bank and records are in yet; a check puts them there. */
  readiness: () => fetch("/api/readiness").then((r) => read<{ ready: boolean; missing: ("bank" | "records")[] }>(r)),
  /** The forecast as it stands now: the same one the orchestrator and the Chat use once a review is signed. */
  forecast: () => fetch("/api/forecast").then((r) => read<Forecast>(r)),
};

export type ChatTurn = { role: "user" | "assistant"; content: string };
/** A button under a Chat answer, to a change on the dashboard (the Chat can't approve anything itself). */
export type ChatLink = { label: string; href: string };
/** A part of the dashboard an answer relates to, picked by rules on the server (never by the model). */
export type ChatSection = { id: string; label: string; href: string; detail: string };
/** A card under a Chat answer: "bookkeeper" is the Bookkeeper's, where the owner runs it. */
export type ChatAction = "bookkeeper";
export type ChatEvent =
  /** A lookup the Chat is making, and where it looks ("Bank (Nessie)", "Your records", "Forecast", "Dashboard"...). */
  | { type: "tool"; text: string; source?: string }
  | { type: "answer"; text: string; sources: string[]; off_topic?: boolean; links?: ChatLink[]; sections?: ChatSection[]; actions?: ChatAction[] }
  /** Asked before any data is in: the chat holds the question and offers an upload or the sample books. */
  | { type: "needs_data"; text: string; missing: ("bank" | "records")[] }
  | { type: "error"; text: string };

/** Asks the chat a question with the conversation so far; streams what it looks at, then the answer. */
/** `signal` stops it: the request and its stream end, with an AbortError. */
export async function askChat(messages: ChatTurn[], onEvent: (e: ChatEvent) => void, signal?: AbortSignal) {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages }),
    signal,
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
