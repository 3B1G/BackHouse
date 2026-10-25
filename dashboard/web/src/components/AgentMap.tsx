import { IconCheck } from "@tabler/icons-react";
import { motion, useReducedMotion } from "motion/react";
import { useEffect, useMemo, useRef, useState } from "react";
import { LiveOrb } from "@/components/ui/live-orb";
import { seconds, traceKey, type TraceEvent, type TraceKind } from "../trace";

// The user as a live orb in the check's ink, its eyes in paper: the colors the user's pill had (index.css --color-ink
// and --color-paper; the orb's shader takes hex).
const ORB = { color: "#1f4d36", eyeColor: "#fcfcfa" };

// Who calls whom, drawn as two tidy trees. You start a check (the Orchestrator calls six agents in order, then has Changes
// dry-run the fixes it suggests) or ask a question (the Chat agent screens it, thinks with ASI:One and uses read-only
// tools). Each hub has one trunk and each agent its own branch, so no two connections cross. Calls travel out as blue
// dots, answers come back as green ones.

type Kind = "you" | "hub" | "agent" | "later";
type MapNode = { id: string; label: string; role: string; x: number; y: number; w: number; h: number; kind: Kind };
type Pt = [number, number];

const ROW = 54;
const checkY = (i: number) => 44 + i * ROW; // 7 rows: 44 ... 368
const chatY = (i: number) => checkY(7) + 18 + i * ROW; // 3 rows: 440, 494, 548
const AGENT = { x: 640, w: 260, h: 44 }; // left edge 510, right edge 770
const HUB = { x: 300, w: 220, h: 70 }; // left edge 190, right edge 410
const TRUNK = 450; // where each hub's branches split
const YOU_TRUNK = 152;
const CHANNEL = 850; // the right-hand lane for the Chat's what-ifs
const ORCH_Y = (checkY(0) + checkY(6)) / 2;
const CHAT_Y = (chatY(0) + chatY(2)) / 2;
const YOU_Y = (ORCH_Y + CHAT_Y) / 2;
const VIEW_H = chatY(2) + 52;

const agent = (id: string, role: string, y: number, kind: Kind = "agent"): MapNode => ({ id, label: id, role, x: AGENT.x, y, w: AGENT.w, h: AGENT.h, kind });
const NODES: MapNode[] = [
  { id: "you", label: "User", role: "The owner", x: 70, y: YOU_Y, w: 76, h: 76, kind: "you" },
  { id: "orchestrator", label: "Orchestrator", role: "Runs checks, suggests fixes", x: HUB.x, y: ORCH_Y, w: HUB.w, h: HUB.h, kind: "hub" },
  { id: "chat", label: "Chat agent", role: "Answers your questions", x: HUB.x, y: CHAT_Y, w: HUB.w, h: HUB.h, kind: "hub" },
  agent("Nessie", "Capital One's bank API", checkY(0)),
  agent("Records", "Builds the books", checkY(1)),
  agent("Matcher", "Bank against books", checkY(2)),
  agent("Bookkeeper", "Fetch.ai agent, checks findings", checkY(3)),
  agent("Forecaster", "Plays out 1,000 futures", checkY(4)),
  agent("Market scout", "Prices on nearby menus", checkY(5)),
  agent("Changes", "Dry runs; acts on your approval", checkY(6)),
  agent("Scope check", "Is it about your money?", chatY(0)),
  agent("ASI:One", "Fetch.ai's model", chatY(1)),
  agent("Tools", "16 read-only tools", chatY(2)),
];
const BY_ID = Object.fromEntries(NODES.map((n) => [n.id, n]));
const left = (id: string) => BY_ID[id].x - BY_ID[id].w / 2;
const right = (id: string) => BY_ID[id].x + BY_ID[id].w / 2;

type Edge = { from: string; to: string; pts: Pt[]; badge?: string; later?: boolean; side?: boolean };
const branch = (hub: string, to: string, extra: Partial<Edge> = {}): Edge => ({
  from: hub,
  to,
  pts: [
    [right(hub), BY_ID[hub].y],
    [TRUNK, BY_ID[hub].y],
    [TRUNK, BY_ID[to].y],
    [left(to), BY_ID[to].y],
  ],
  ...extra,
});
const fromYou = (to: string): Edge => ({
  from: "you",
  to,
  pts: [
    [right("you"), YOU_Y],
    [YOU_TRUNK, YOU_Y],
    [YOU_TRUNK, BY_ID[to].y],
    [left(to), BY_ID[to].y],
  ],
});
// The numbers are the check's order, as on How it works. Changes has none: it dry-runs the Orchestrator's own suggestions.
const EDGES: Edge[] = [
  fromYou("orchestrator"),
  fromYou("chat"),
  ...["Nessie", "Records", "Matcher", "Bookkeeper", "Forecaster", "Market scout"].map((to, i) => branch("orchestrator", to, { badge: String(i + 1) })),
  branch("orchestrator", "Changes"),
  ...["Scope check", "ASI:One", "Tools"].map((to) => branch("chat", to)),
  {
    from: "Tools",
    to: "Forecaster",
    side: true,
    pts: [
      [right("Tools"), BY_ID.Tools.y],
      [CHANNEL, BY_ID.Tools.y],
      [CHANNEL, BY_ID.Forecaster.y],
      [right("Forecaster"), BY_ID.Forecaster.y],
    ],
  },
];

/** A polyline with softly rounded corners. */
function rounded(pts: Pt[], r = 9) {
  let d = `M${pts[0][0]} ${pts[0][1]}`;
  for (let i = 1; i < pts.length - 1; i++) {
    const [px, py] = pts[i - 1];
    const [x, y] = pts[i];
    const [nx, ny] = pts[i + 1];
    const a = Math.min(r, Math.hypot(x - px, y - py) / 2);
    const b = Math.min(r, Math.hypot(nx - x, ny - y) / 2);
    if (a < 0.5 || b < 0.5) {
      d += `L${x} ${y}`;
      continue;
    }
    const ux = Math.sign(x - px) * a;
    const uy = Math.sign(y - py) * a;
    const vx = Math.sign(nx - x) * b;
    const vy = Math.sign(ny - y) * b;
    d += `L${x - ux} ${y - uy}Q${x} ${y} ${x + vx} ${y + vy}`;
  }
  const [lx, ly] = pts[pts.length - 1];
  return `${d}L${lx} ${ly}`;
}

/** Points spaced evenly along a polyline, for a dot moving at a steady speed. */
function along(pts: Pt[], n = 26): Pt[] {
  const seg = pts.slice(1).map((p, i) => Math.hypot(p[0] - pts[i][0], p[1] - pts[i][1]));
  const total = seg.reduce((a, b) => a + b, 0) || 1;
  return Array.from({ length: n + 1 }, (_, k) => {
    let d = (total * k) / n;
    let i = 0;
    while (i < seg.length - 1 && d > seg[i]) d -= seg[i++];
    const t = seg[i] ? d / seg[i] : 0;
    return [pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t] as Pt;
  });
}

const OUT: TraceKind[] = ["call", "start"];
const BACK: TraceKind[] = ["result", "done", "waiting", "error"];

/** Who an event is between: [caller, callee]. Calls go out along that connection; answers come back. */
function route(e: TraceEvent): [string, string] | null {
  if (e.kind === "log" || e.kind === "check") return null;
  if (e.agent === "Pipeline" || e.agent === "Orchestrator") return ["you", "orchestrator"];
  if (e.agent === "Chat") return e.kind === "call" || e.kind === "result" ? ["chat", "ASI:One"] : ["you", "chat"];
  if (e.agent === "Scope check" || e.agent === "Tools") return ["chat", e.agent];
  if (!BY_ID[e.agent]) return null;
  const g = e.group ?? "";
  // The Chat reaches the Forecaster through its what-if tool; a check's forecast comes from the orchestrator or the pipeline.
  return [g.startsWith("chat-") ? "Tools" : /^[0-9a-f]{10}$/.test(g) ? "orchestrator" : "you", e.agent];
}

type NodeState = { open: number; calls: number; lastMs: number | null; lastAt: number; lastOut: number; lastKind: TraceKind | null };

function tally(events: TraceEvent[]) {
  const nodes: Record<string, NodeState> = {};
  for (const n of NODES) nodes[n.id] = { open: 0, calls: 0, lastMs: null, lastAt: 0, lastOut: 0, lastKind: null };
  const edges: Record<string, number> = {};
  for (const e of events) {
    const r = route(e);
    if (!r) continue;
    const s = nodes[r[1]];
    const k = `${r[0]}>${r[1]}`;
    if (OUT.includes(e.kind)) {
      s.open += 1;
      if (e.kind === "call") s.calls += 1; // a step starting isn't a call; the scripts and requests inside it are
      s.lastOut = e.at;
      edges[k] = (edges[k] ?? 0) + 1;
    } else if (BACK.includes(e.kind)) {
      s.open = Math.max(0, s.open - 1);
      edges[k] = Math.max(0, (edges[k] ?? 0) - 1);
      Object.assign(s, { lastMs: e.ms, lastAt: e.at, lastKind: e.kind });
    }
  }
  return { nodes, edges };
}

type Packet = { id: string; pts: Pt[]; color: string };

function Dot({ p, onDone }: { p: Packet; onDone: () => void }) {
  return (
    <motion.circle
      r={6.5}
      fill={p.color}
      stroke="var(--color-paper)"
      strokeWidth={2}
      initial={{ cx: p.pts[0][0], cy: p.pts[0][1], opacity: 0 }}
      animate={{ cx: p.pts.map((q) => q[0]), cy: p.pts.map((q) => q[1]), opacity: [0, 1, 1, 1, 0.9, 0] }}
      transition={{ duration: 0.9, ease: "easeInOut" }}
      onAnimationComplete={onDone}
    />
  );
}

function Box({ n, s, now }: { n: MapNode; s: NodeState; now: number }) {
  const working = s.open > 0 && now - s.lastOut < 90;
  const failed = s.lastKind === "error" && now - s.lastAt < 30;
  const justDone = !working && !failed && s.lastAt > 0 && now - s.lastAt < 4;
  const x = n.x - n.w / 2;
  const y = n.y - n.h / 2;
  const later = n.kind === "later";
  if (n.kind === "you")
    return (
      <g>
        <foreignObject x={x} y={y} width={n.w} height={n.h}>
          <LiveOrb variant="custom" color={ORB.color} eyeColor={ORB.eyeColor} size={n.w} label={`${n.label}: ${n.role.toLowerCase()}`} />
        </foreignObject>
        <text x={n.x} y={y + n.h + 24} textAnchor="middle" className="fill-ink font-display text-[22px] font-extrabold [font-stretch:80%]">
          {n.label}
        </text>
      </g>
    );
  const status = later
    ? n.role
    : working
      ? n.id === "orchestrator"
        ? "Running the check…"
        : n.id === "chat"
          ? "Answering…"
          : "Working…"
      : failed
        ? "Stopped with an error"
        : s.calls
          ? `${s.calls} call${s.calls === 1 ? "" : "s"}, last took ${seconds(s.lastMs ?? 0)}`
          : n.role;
  const hub = n.kind === "hub";
  return (
    <g>
      {working && <rect x={x - 6} y={y - 6} width={n.w + 12} height={n.h + 12} rx={14} fill="var(--color-check-deep)" className="motion-safe:animate-pulse" />}
      <rect
        x={x}
        y={y}
        width={n.w}
        height={n.h}
        rx={hub ? 12 : 9}
        fill={hub ? "var(--color-check)" : "var(--color-paper)"}
        stroke={failed ? "var(--color-serial)" : working || hub ? "var(--color-ink)" : later ? "var(--color-ghost)" : "var(--color-rule)"}
        strokeWidth={working ? 2.5 : 2}
        strokeDasharray={later ? "6 5" : undefined}
      />
      <text
        x={x + 14}
        y={y + (hub ? 31 : 19)}
        className={hub ? "fill-ink font-display text-[22px] font-extrabold [font-stretch:80%]" : `text-[15px] font-bold ${later ? "fill-pencil" : "fill-carbon"}`}
      >
        {n.label}
      </text>
      <text x={x + 14} y={y + (hub ? 54 : 36)} className={`text-[12.5px] ${working ? "fill-ink font-bold" : failed ? "fill-serial font-bold" : "fill-pencil"}`}>
        {status}
      </text>
      {justDone && (
        <motion.g initial={{ scale: 0, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} transition={{ type: "spring", stiffness: 500, damping: 20 }}>
          <IconCheck x={x + n.w - 32} y={n.y - 11} width={22} height={22} stroke={2.5} className="text-ink" />
        </motion.g>
      )}
    </g>
  );
}

export function AgentMap({ events, now }: { events: TraceEvent[]; now: number }) {
  const reduce = useReducedMotion();
  const { nodes, edges } = useMemo(() => tally(events), [events]);
  const [packets, setPackets] = useState<Packet[]>([]);
  const last = useRef<string | null>(null);

  // New calls and answers become dots on their connection. History replayed on page load stays still.
  useEffect(() => {
    const i = last.current ? events.findIndex((e) => traceKey(e) === last.current) : -1;
    const fresh = events.slice(i + 1);
    if (events.length) last.current = traceKey(events[events.length - 1]);
    if (reduce) return;
    const t = Date.now() / 1000;
    const add: Packet[] = [];
    for (const e of fresh) {
      const r = t - e.at < 2.5 ? route(e) : null;
      const edge = r && EDGES.find((x) => x.from === r[0] && x.to === r[1]);
      if (!edge) continue;
      const back = BACK.includes(e.kind);
      const pts = along(edge.pts);
      add.push({
        id: traceKey(e),
        pts: back ? [...pts].reverse() : pts,
        color: e.kind === "error" ? "var(--color-serial)" : back ? "var(--color-ink)" : "var(--color-ballpoint)",
      });
    }
    if (add.length) setPackets((p) => [...p, ...add].slice(-40));
  }, [events, reduce]);

  return (
    <div role="region" aria-label="Map of which agent calls which" tabIndex={0} className="mt-4 overflow-x-auto rounded-lg border border-rule bg-paper">
      <svg
        viewBox={`0 0 1000 ${VIEW_H}`}
        className="mx-auto block h-auto w-full min-w-[46rem] max-w-[64rem]"
        role="img"
        aria-label="When the user runs a check, the Orchestrator calls Nessie, Records, the Matcher, the Bookkeeper, the Forecaster and Market scout in that order, then has Changes dry-run the fixes it suggests. When the user asks a question, the Chat agent runs a scope check, thinks with ASI:One and uses read-only tools, including the Forecaster for what-ifs."
      >
        <text x={left("orchestrator")} y={ORCH_Y - HUB.h / 2 - 12} className="fill-pencil text-[13px] font-bold">
          When the user runs a check
        </text>
        <text x={left("chat")} y={CHAT_Y - HUB.h / 2 - 12} className="fill-pencil text-[13px] font-bold">
          When the user asks a question
        </text>
        {EDGES.map((e) => {
          const live = !e.later && (edges[`${e.from}>${e.to}`] ?? 0) > 0;
          return (
            <path
              key={`${e.from}>${e.to}`}
              d={rounded(e.pts)}
              fill="none"
              strokeLinecap="round"
              strokeLinejoin="round"
              stroke={live ? "var(--color-ink)" : e.later || e.side ? "var(--color-ghost)" : "var(--color-rule)"}
              strokeWidth={live ? 2.75 : 2}
              strokeDasharray={live ? undefined : e.later ? "6 5" : e.side ? "2 6" : undefined}
              className={live ? "edge-live" : undefined}
            />
          );
        })}
        {EDGES.filter((e) => e.badge).map((e) => (
          <g key={`badge-${e.to}`}>
            <circle cx={(TRUNK + left(e.to)) / 2} cy={BY_ID[e.to].y} r={10.5} fill="var(--color-paper)" stroke="var(--color-ink)" strokeWidth={1.5} />
            <text x={(TRUNK + left(e.to)) / 2} y={BY_ID[e.to].y + 4.5} textAnchor="middle" className="fill-ink text-[12px] font-bold">
              {e.badge}
            </text>
          </g>
        ))}
        <text x={CHANNEL + 10} y={(BY_ID.Forecaster.y + BY_ID.Tools.y) / 2} className="fill-pencil text-[13px]">
          <tspan x={CHANNEL + 10}>The Chat's</tspan>
          <tspan x={CHANNEL + 10} dy="1.3em">
            what-ifs
          </tspan>
        </text>
        {NODES.map((n) => (
          <Box key={n.id} n={n} s={nodes[n.id]} now={now} />
        ))}
        {packets.map((p) => (
          <Dot key={p.id} p={p} onDone={() => setPackets((list) => list.filter((x) => x.id !== p.id))} />
        ))}
      </svg>
    </div>
  );
}
