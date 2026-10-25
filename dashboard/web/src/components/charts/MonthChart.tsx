import type { PointerEvent } from "react";
import type { Forecast } from "../../api";
import { change, compactMoney, money, monthName, monthYear } from "../../format";
import { inside, linear, niceTicks, pageScale, useScrub, useWidth } from "./chart";
import { Legend, LineKey, SwatchKey, TableFrame, Tooltip, ViewTitle } from "./ChartBits";

/** One month: money in minus money out. Past months are the bank's; future ones are the forecast's likely change. */
type Bar = {
  month: string;
  past: boolean;
  v: number;
  lo: number;
  hi: number;
  inCents: number;
  outCents: number;
  through: string | null;
};

function bars(f: Forecast): Bar[] {
  const last = f.window_end.slice(0, 7);
  return [
    ...f.history.months.map((m) => ({
      month: m.month, past: true, v: m.net_cents, lo: m.net_cents, hi: m.net_cents,
      inCents: m.in_cents, outCents: m.out_cents, through: m.through ?? null,
    })),
    ...f.months
      .filter((m) => m.month <= last)
      .map((m) => ({
        month: m.month, past: false, v: m.p50, lo: m.p10, hi: m.p90,
        inCents: 0, outCents: 0, through: null,
      })),
  ];
}

/** "October 1 to 24, 2026" for the month still under way, else "September 2026". */
const named = (b: Bar) => (b.through ? `${monthName(b.month)} 1 to ${+b.through.slice(8, 10)}, ${b.month.slice(0, 4)}` : monthYear(b.month));

function readout(b: Bar) {
  if (b.past) return `${named(b)}: ${change(b.v)}. ${money(b.inCents)} came in, ${money(b.outCents)} went out.`;
  return `${monthYear(b.month)} forecast: likely ${change(b.v)}, ${change(b.lo)} to ${change(b.hi)} in 8 of 10 futures.`;
}

/** A bar from the zero line to its value, rounded only at the value end. */
function barPath(cx: number, y0: number, y1: number, w: number) {
  const h = Math.abs(y1 - y0);
  if (h < 0.5) return "";
  const r = Math.min(4, h, w / 2);
  const l = cx - w / 2;
  const rt = cx + w / 2;
  const s = y1 < y0 ? 1 : -1; // 1 when the bar grows up
  return `M${l} ${y0}V${y1 + s * r}Q${l} ${y1} ${l + r} ${y1}H${rt - r}Q${rt} ${y1} ${rt} ${y1 + s * r}V${y0}Z`;
}

export function MonthChart({ f, table }: { f: Forecast; table: boolean }) {
  const list = bars(f);
  return (
    <figure className="m-0">
      <ViewTitle title="How much cash grew or shrank each month">
        Money in minus money out. The past year is from your bank; the shaded months are the Forecaster's likely change.
      </ViewTitle>
      {table ? (
        <TableFrame label="Cash change by month, as a table" head={["Month", "Change", "Details"]}>
          {list.map((b) => (
            <tr key={b.month}>
              <th scope="row">{b.past ? named(b) : `${monthYear(b.month)} (forecast)`}</th>
              <td>{b.past ? change(b.v) : `${change(b.v)} likely`}</td>
              <td className="text-pencil">
                {b.past ? `${money(b.inCents)} in, ${money(b.outCents)} out` : `${change(b.lo)} to ${change(b.hi)} in 8 of 10 futures`}
              </td>
            </tr>
          ))}
        </TableFrame>
      ) : (
        <>
          <Legend
            items={[
              { key: <SwatchKey color="var(--color-ballpoint)" />, label: "Cash grew" },
              { key: <SwatchKey color="var(--color-serial)" />, label: "Cash shrank" },
            ]}
          />
          <Plot list={list} />
        </>
      )}
    </figure>
  );
}

function Plot({ list }: { list: Bar[] }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const firstAhead = list.findIndex((b) => !b.past);
  const scrub = useScrub(list.length, Math.max(0, firstAhead - 1));
  const s = pageScale();
  const narrow = width < 520;
  const H = narrow ? 240 : Math.round(Math.min(420, Math.max(290, width * 0.36)));
  const M = { top: Math.round(30 * s), right: 8, bottom: Math.round(30 * s), left: Math.round((narrow ? 44 : 52) * s) };
  const right = Math.max(M.left + 1, width - M.right);
  const slot = (right - M.left) / list.length;
  const bw = Math.max(3, Math.min(24 * s, slot * 0.62));
  const values = list.map((b) => b.v);
  const hi = Math.max(0, ...values);
  const lo = Math.min(0, ...values);
  const pad = (hi - lo) * 0.12;
  const y = linear(lo - pad, hi + pad, H - M.bottom, M.top);
  const cx = (i: number) => M.left + slot * (i + 0.5);
  const divider = M.left + slot * (firstAhead < 0 ? list.length : firstAhead);
  const worst = list.reduce((a, b, i) => (b.v < list[a].v ? i : a), 0);
  const best = list.reduce((a, b, i) => (b.v > list[a].v ? i : a), 0);
  const every = narrow ? 6 : 3;
  const at = scrub.at;
  const b = at === null ? null : list[at];

  const onPointer = (e: PointerEvent<SVGRectElement>) => {
    const box = e.currentTarget.getBoundingClientRect();
    scrub.hover(Math.max(0, Math.min(list.length - 1, Math.floor((e.clientX - box.left) / slot))));
  };
  const valueLabel = (i: number) => {
    const v = list[i].v;
    return (
      <text
        key={`value-${i}`}
        x={Math.max(M.left + 22 * s, Math.min(right - 22 * s, cx(i)))}
        y={v < 0 ? y(v) + 15 * s : y(v) - 7 * s}
        textAnchor="middle"
        className="fill-carbon text-[0.75rem] font-bold"
      >
        {v < 0 ? "−" : "+"}
        {compactMoney(Math.abs(v))}
      </text>
    );
  };

  return (
    <div
      ref={ref}
      role="group"
      aria-label="Monthly chart. Use the left and right arrow keys to read each month."
      className="relative mt-3 rounded-md"
      {...scrub.focusProps}
    >
      {width > 0 && (
        <svg width={width} height={H} aria-hidden="true" className="block select-none" style={{ touchAction: "pan-y" }}>
          <rect x={divider} y={M.top} width={right - divider} height={H - M.bottom - M.top} fill="var(--color-ahead)" />
          <text x={divider - 8} y={M.top - 12 * s} textAnchor="end" className="fill-pencil text-[0.75rem] font-bold">
            Past year
          </text>
          <text x={divider + 8} y={M.top - 12 * s} className="fill-pencil text-[0.75rem] font-bold">
            Forecast
          </text>
          {niceTicks(lo - pad, hi + pad, narrow ? 4 : 5)
            .filter((v) => v !== 0)
            .map((v) => (
              <g key={v}>
                <line x1={M.left} x2={right} y1={y(v)} y2={y(v)} stroke="var(--color-rule)" strokeOpacity={0.6} />
                <text x={M.left - 8} y={y(v)} dy="0.32em" textAnchor="end" className="fill-pencil text-[0.75rem]">
                  {compactMoney(v)}
                </text>
              </g>
            ))}
          <text x={M.left - 8} y={y(0)} dy="0.32em" textAnchor="end" className="fill-pencil text-[0.75rem]">
            $0
          </text>
          {list.map((d, i) => (
            <path
              key={d.month}
              d={barPath(cx(i), y(0), y(d.v), bw)}
              fill={d.v < 0 ? "var(--color-serial)" : "var(--color-ballpoint)"}
              opacity={at !== null && at !== i ? 0.45 : 1}
            />
          ))}
          <line x1={M.left} x2={right} y1={y(0)} y2={y(0)} stroke="var(--color-carbon)" strokeOpacity={0.7} />
          {valueLabel(worst)}
          {list[best].v > 0 && valueLabel(best)}
          {list.map((d, i) => {
            const m = +d.month.slice(5, 7) - 1;
            if (m % every !== 0) return null;
            const at = inside(cx(i), width, 28 * s);
            return (
              <text key={`tick-${d.month}`} x={at.x} y={H - 9 * s} textAnchor={at.anchor} className="fill-pencil text-[0.75rem]">
                {m === 0 ? `Jan ${d.month.slice(0, 4)}` : monthName(d.month).slice(0, 3)}
              </text>
            );
          })}
          <rect
            x={M.left}
            y={M.top}
            width={right - M.left}
            height={H - M.bottom - M.top}
            fill="transparent"
            onPointerMove={onPointer}
            onPointerDown={onPointer}
            onPointerLeave={() => scrub.hover(null)}
          />
        </svg>
      )}
      {b && at !== null && (
        <Tooltip x={cx(at)} width={width}>
          <p className="text-pencil">{b.past ? named(b) : `${monthYear(b.month)} forecast`}</p>
          <p className="mt-0.5 flex items-center gap-2">
            <LineKey color={b.v < 0 ? "var(--color-serial)" : "var(--color-ballpoint)"} />
            <span>
              <strong className="text-base">{change(b.v)}</strong>{" "}
              <span className="text-pencil">{b.past ? (b.v < 0 ? "cash shrank" : "cash grew") : "likely"}</span>
            </span>
          </p>
          {b.past ? (
            <>
              <p className="mt-0.5 pl-[30px]">
                {money(b.inCents)} <span className="text-pencil">came in</span>
              </p>
              <p className="mt-0.5 pl-[30px]">
                {money(b.outCents)} <span className="text-pencil">went out</span>
              </p>
            </>
          ) : (
            <p className="mt-0.5 pl-[30px]">
              {change(b.lo)} to {change(b.hi)} <span className="text-pencil">in 8 of 10 futures</span>
            </p>
          )}
        </Tooltip>
      )}
      <p aria-live="polite" className="sr-only">
        {scrub.keyed && b ? readout(b) : ""}
      </p>
    </div>
  );
}
