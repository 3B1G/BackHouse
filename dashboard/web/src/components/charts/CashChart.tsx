import { useId, type PointerEvent } from "react";
import type { Forecast } from "../../api";
import { cash, compactMoney, mediumDate, percent } from "../../format";
import { DAY, inside, linear, monthTicks, nearest, niceTicks, pageScale, path, time, useScrub, useWidth } from "./chart";
import { Legend, LineKey, SwatchKey, TableFrame, Tooltip, ViewTitle } from "./ChartBits";

/** One payday: the lowest balance until the next payday. Past ones are the bank's; future ones are the forecast's
 * likely path (y) and its bad and good case (lo, hi: the 10th and 90th percentile of the futures). */
type Point = { t: number; date: string; past: boolean; y: number; lo: number; hi: number; clears: number };

function points(f: Forecast): Point[] {
  const end = time(f.window_end);
  return [
    ...f.history.paydays.map((p) => ({
      t: time(p.date), date: p.date, past: true, y: p.low_cents, lo: p.low_cents, hi: p.low_cents, clears: 1,
    })),
    ...f.paydays
      .filter((p) => time(p.date) <= end)
      .map((p) => ({
        t: time(p.date), date: p.date, past: false, y: p.p50, lo: p.p10, hi: p.p90, clears: p.clears,
      })),
  ];
}

function readout(p: Point) {
  if (p.past) return `${mediumDate(p.date)}: ${cash(p.y)} left after payroll.`;
  return `${mediumDate(p.date)} payday: likely ${cash(p.y)} left after payroll, ${cash(p.hi)} in the good case and ${cash(p.lo)} in the bad case. ${percent(p.clears)} chance payroll clears.`;
}

/** Where a path runs out: a ring on the band's edges (bad and good case), a red dot on the likely line. */
function Marker({ ring = false }: { ring?: boolean }) {
  return (
    <svg aria-hidden="true" width="14" height="14" className="shrink-0">
      <circle
        cx="7"
        cy="7"
        r={ring ? 4.5 : 5}
        fill={ring ? "var(--color-paper)" : "var(--color-serial)"}
        stroke={ring ? "var(--color-ballpoint)" : "none"}
        strokeWidth="2"
      />
    </svg>
  );
}

/** The three run-out dates in words, under the chart (and under the table). */
function RunOut({ f }: { f: Forecast }) {
  const items = [
    { label: "Bad case", date: f.runout.bad, ring: true },
    { label: "Likely", date: f.runout.likely, ring: false },
    { label: "Good case", date: f.runout.good, ring: true },
  ];
  return (
    <p className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1.5 text-[0.9375rem] text-carbon">
      <span className="text-pencil">Cash runs out:</span>
      {items.map((it) => (
        <span key={it.label} className="inline-flex items-center gap-1.5">
          <Marker ring={it.ring} />
          <strong>{it.label}</strong> {it.date ? mediumDate(it.date) : "beyond two years"}
        </span>
      ))}
    </p>
  );
}

export function CashChart({ f, table }: { f: Forecast; table: boolean }) {
  const pts = points(f);
  const past = f.history.paydays;
  const first = past[0];
  const last = past[past.length - 1];
  return (
    <figure className="m-0">
      <ViewTitle title="Cash left after each payroll">
        The past year is from your bank. After that come the Forecaster's 1,000 possible futures: the line is the likely path, and the
        band holds 8 in 10 of them, from the bad case at its bottom edge to the good case at its top.
      </ViewTitle>
      <p className="sr-only">
        After payroll, cash went from {cash(first.low_cents)} on {mediumDate(first.date)} to {cash(last.low_cents)} on{" "}
        {mediumDate(last.date)}.
      </p>
      {table ? (
        <TableFrame
          label="Cash left after each payroll, as a table"
          head={["Payday", "Left after payroll", "Bad case to good case", "Payroll clears"]}
        >
          {pts.map((p) => (
            <tr key={p.date}>
              <th scope="row">{mediumDate(p.date)}</th>
              <td>{p.past ? cash(p.y) : `${cash(p.y)} likely`}</td>
              <td className="text-pencil">{p.past ? "From your bank" : `${cash(p.lo)} to ${cash(p.hi)}`}</td>
              <td>{p.past ? "Paid" : percent(p.clears)}</td>
            </tr>
          ))}
        </TableFrame>
      ) : (
        <>
          <Legend
            items={[
              { key: <LineKey color="var(--color-ballpoint)" />, label: "Cash left after payroll" },
              { key: <SwatchKey color="var(--color-ballpoint)" opacity={0.25} />, label: "8 in 10 futures" },
              { key: <LineKey color="var(--color-serial)" dashed />, label: "One payroll plus rent" },
            ]}
          />
          <Plot f={f} pts={pts} lastPast={past.length - 1} />
        </>
      )}
      <RunOut f={f} />
    </figure>
  );
}

type Box = { l: number; t: number; r: number; b: number };
const clash = (a: Box, b: Box) => a.l < b.r && b.l < a.r && a.t < b.b && b.t < a.b;

function Plot({ f, pts, lastPast }: { f: Forecast; pts: Point[]; lastPast: number }) {
  const clip = useId();
  const [ref, width] = useWidth<HTMLDivElement>();
  const scrub = useScrub(pts.length, lastPast);
  const s = pageScale();
  const narrow = width < 520;
  const H = narrow ? 260 : Math.round(Math.min(480, Math.max(320, width * 0.4)));
  const M = { top: Math.round(30 * s), right: 12, bottom: Math.round(30 * s), left: Math.round((narrow ? 44 : 52) * s) };
  const charW = 6.8 * s; // bold 12px Archivo, scaled with the page
  const lineH = 14 * s;
  const right = Math.max(M.left + 1, width - M.right);
  const plotH = H - M.bottom - M.top;

  const top = Math.max(f.cash_today_cents, ...pts.map((p) => p.hi)) * 1.06;
  const lowest = Math.min(0, ...pts.map((p) => p.lo));
  const bottom = Math.max(lowest, -0.42 * top) - 0.04 * top; // a deep shortfall is cut off so it can't flatten the rest
  const y = linear(bottom, top, H - M.bottom, M.top);
  const x0 = pts[0].t - 8 * DAY;
  const x1 = time(f.window_end);
  const x = linear(x0, x1, M.left, right);
  const xs = pts.map((p) => x(p.t));
  const today = x(time(f.as_of));
  const zero = y(0);
  const clampY = (v: number) => Math.max(M.top, Math.min(H - M.bottom, y(v)));

  const ahead = pts.slice(lastPast); // starts at the last real payday, so the forecast continues the line
  const line = path(pts.map((p) => [x(p.t), y(p.y)]));
  const goodEdge = path(ahead.map((p) => [x(p.t), y(p.hi)]));
  const badEdge = path(ahead.map((p) => [x(p.t), y(p.lo)]));
  const band = `${goodEdge}${path([...ahead].reverse().map((p) => [x(p.t), y(p.lo)])).replace("M", "L")}Z`;

  /** A path's value on any day, read between paydays. */
  const valueAt = (t: number, key: "lo" | "y" | "hi") => {
    for (let i = 1; i < ahead.length; i++) {
      const a = ahead[i - 1];
      const b = ahead[i];
      if (t <= b.t) return a[key] + ((b[key] - a[key]) * (t - a.t)) / (b.t - a.t || 1);
    }
    return ahead[ahead.length - 1][key];
  };

  // Each path's run-out marker sits on that path; its name goes in the first nearby corner that is free.
  const zoneLabelY = Math.min(H - M.bottom - 8 * s, zero + 18 * s);
  const taken: Box[] =
    zero < H - M.bottom ? [{ l: M.left + 4, t: zoneLabelY - lineH, r: M.left + 14 + 11 * charW, b: zoneLabelY + 4 }] : [];
  const runouts = (
    [
      { id: "likely", label: "Likely", date: f.runout.likely, key: "y", ring: false, corners: ["above-right", "below-left", "below-right", "above-left"] },
      { id: "bad", label: "Bad case", date: f.runout.bad, key: "lo", ring: true, corners: ["below-left", "below-right", "above-left", "above-right"] },
      { id: "good", label: "Good case", date: f.runout.good, key: "hi", ring: true, corners: ["below-left", "above-left", "below-right", "above-right"] },
    ] as const
  )
    .filter((m) => m.date !== null && time(m.date) >= ahead[0].t && time(m.date) <= x1)
    .map((m) => {
      const t = time(m.date as string);
      return { ...m, cx: x(t), cy: clampY(valueAt(t, m.key)) };
    });
  for (const m of runouts) taken.push({ l: m.cx - 6, t: m.cy - 6, r: m.cx + 6, b: m.cy + 6 });
  const marks = runouts.map((m) => {
    const w = m.label.length * charW + 2;
    for (const corner of m.corners) {
      const l = corner.endsWith("left") ? m.cx - 9 - w : m.cx + 9;
      const t = corner.startsWith("above") ? m.cy - 9 - lineH : m.cy + 9;
      const box = { l, t, r: l + w, b: t + lineH };
      if (box.l < M.left + 2 || box.r > right - 2 || box.t < M.top + 2 || box.b > H - M.bottom - 2) continue;
      if (taken.some((b) => clash(b, box))) continue;
      taken.push(box);
      return { ...m, box };
    }
    return { ...m, box: null }; // no room: the line under the chart still names it
  });

  const at = scrub.at;
  const p = at === null ? null : pts[at];
  const onPointer = (e: PointerEvent<SVGRectElement>) => {
    const box = e.currentTarget.getBoundingClientRect();
    scrub.hover(nearest(xs, e.clientX - box.left + M.left));
  };

  return (
    <div
      ref={ref}
      role="group"
      aria-label="Cash chart. Use the left and right arrow keys to read each payday."
      className="relative mt-3 rounded-md"
      {...scrub.focusProps}
    >
      {width > 0 && (
        <svg width={width} height={H} aria-hidden="true" className="block select-none" style={{ touchAction: "pan-y" }}>
          <defs>
            <clipPath id={clip}>
              <rect x={M.left} y={M.top} width={right - M.left} height={plotH} />
            </clipPath>
          </defs>
          <rect x={today} y={M.top} width={right - today} height={plotH} fill="var(--color-ahead)" />
          {zero < H - M.bottom && (
            <>
              <rect x={M.left} y={zero} width={right - M.left} height={H - M.bottom - zero} fill="var(--color-serial-soft)" fillOpacity={0.8} />
              <text x={M.left + 8} y={zoneLabelY} className="fill-serial text-[0.75rem] font-bold">
                Out of cash
              </text>
            </>
          )}
          {niceTicks(bottom, top, narrow ? 4 : 6).map((v) => (
            <g key={v}>
              <line
                x1={M.left}
                x2={right}
                y1={y(v)}
                y2={y(v)}
                stroke={v === 0 ? "var(--color-serial-mid)" : "var(--color-rule)"}
                strokeOpacity={v === 0 ? 1 : 0.6}
              />
              <text x={M.left - 8} y={y(v)} dy="0.32em" textAnchor="end" className="fill-pencil text-[0.75rem]">
                {compactMoney(v)}
              </text>
            </g>
          ))}
          <line x1={M.left} x2={right} y1={y(f.floor_cents)} y2={y(f.floor_cents)} stroke="var(--color-serial)" strokeWidth={1.25} strokeDasharray="5 4" />
          <line x1={today} x2={today} y1={M.top - 8} y2={H - M.bottom} stroke="var(--color-pencil)" strokeOpacity={0.7} />
          <text x={today - 8} y={M.top - 12 * s} textAnchor="end" className="fill-pencil text-[0.75rem] font-bold">
            Past year
          </text>
          <text x={today + 8} y={M.top - 12 * s} className="fill-pencil text-[0.75rem] font-bold">
            Forecast
          </text>
          <g clipPath={`url(#${clip})`}>
            <path d={band} fill="var(--color-ballpoint)" fillOpacity={0.16} />
            <path d={goodEdge} fill="none" stroke="var(--color-ballpoint)" strokeOpacity={0.45} strokeWidth={1} />
            <path d={badEdge} fill="none" stroke="var(--color-ballpoint)" strokeOpacity={0.45} strokeWidth={1} />
            <path d={line} fill="none" stroke="var(--color-ballpoint)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
          </g>
          {marks.map((m) => (
            <g key={m.id}>
              <circle
                cx={m.cx}
                cy={m.cy}
                r={m.ring ? 4.5 : 5}
                fill={m.ring ? "var(--color-paper)" : "var(--color-serial)"}
                stroke={m.ring ? "var(--color-ballpoint)" : "var(--color-paper)"}
                strokeWidth={2}
              />
              {m.box && (
                <text x={m.box.l} y={m.box.b - 3 * s} className="fill-carbon text-[0.75rem] font-bold">
                  {m.label}
                </text>
              )}
            </g>
          ))}
          {monthTicks(x0, x1, narrow ? 6 : 3).map((t) => {
            const at = inside(x(t.t), width, 28 * s);
            return (
              <text key={t.t} x={at.x} y={H - 9 * s} textAnchor={at.anchor} className="fill-pencil text-[0.75rem]">
                {t.label}
              </text>
            );
          })}
          {p && at !== null && (
            <g>
              <line x1={xs[at]} x2={xs[at]} y1={M.top} y2={H - M.bottom} stroke="var(--color-carbon)" strokeOpacity={0.4} />
              <circle cx={xs[at]} cy={clampY(p.y)} r={5} fill="var(--color-ballpoint)" stroke="var(--color-paper)" strokeWidth={2} />
            </g>
          )}
          <rect
            x={M.left}
            y={M.top}
            width={right - M.left}
            height={plotH}
            fill="transparent"
            onPointerMove={onPointer}
            onPointerDown={onPointer}
            onPointerLeave={() => scrub.hover(null)}
          />
        </svg>
      )}
      {p && at !== null && (
        <Tooltip x={xs[at]} width={width}>
          <p className="text-pencil">{p.past ? mediumDate(p.date) : `${mediumDate(p.date)} payday`}</p>
          <p className="mt-0.5 flex items-center gap-2">
            <LineKey color="var(--color-ballpoint)" />
            <span>
              <strong className="text-base">{cash(p.y)}</strong>{" "}
              <span className="text-pencil">{p.past ? "left after payroll" : "likely left after payroll"}</span>
            </span>
          </p>
          {!p.past && (
            <>
              <p className="mt-0.5 pl-[30px]">
                {cash(p.hi)} <span className="text-pencil">good case</span>
              </p>
              <p className="mt-0.5 pl-[30px]">
                {cash(p.lo)} <span className="text-pencil">bad case</span>
              </p>
              <p className="mt-0.5 pl-[30px]">
                <strong>{percent(p.clears)}</strong> <span className="text-pencil">chance payroll clears</span>
              </p>
            </>
          )}
        </Tooltip>
      )}
      <p aria-live="polite" className="sr-only">
        {scrub.keyed && p ? readout(p) : ""}
      </p>
    </div>
  );
}
