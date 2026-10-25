import type { ReactNode } from "react";

/** The readout beside the crosshair. It flips to the left of the pointer past the middle, so it never leaves the chart. */
export function Tooltip({ x, width, children }: { x: number; width: number; children: ReactNode }) {
  const flip = x > width / 2;
  return (
    <div
      aria-hidden="true"
      className="pointer-events-none absolute top-1 z-10 w-max max-w-[16.5rem] rounded-md border border-rule bg-paper px-3 py-2 text-sm leading-snug text-carbon shadow-[0_8px_20px_-8px_rgba(31,77,54,0.45)]"
      style={flip ? { right: width - x + 14 } : { left: x + 14 }}
    >
      {children}
    </div>
  );
}

/** A short stroke of a series' color: legends for lines, and every tooltip row. */
export function LineKey({ color, dashed = false }: { color: string; dashed?: boolean }) {
  return (
    <svg aria-hidden="true" width="22" height="10" className="shrink-0">
      <line x1="2" x2="20" y1="5" y2="5" stroke={color} strokeWidth="2.5" strokeLinecap="round" strokeDasharray={dashed ? "5 4" : undefined} />
    </svg>
  );
}

/** A swatch for bars and areas in a legend. */
export function SwatchKey({ color, opacity = 1 }: { color: string; opacity?: number }) {
  return <span aria-hidden="true" className="inline-block h-3 w-3 shrink-0 rounded-[3px]" style={{ background: color, opacity }} />;
}

export function Legend({ items }: { items: { key: ReactNode; label: string }[] }) {
  return (
    <ul className="mt-3 flex flex-wrap gap-x-5 gap-y-1.5 text-sm text-carbon">
      {items.map((it) => (
        <li key={it.label} className="flex items-center gap-2">
          {it.key}
          {it.label}
        </li>
      ))}
    </ul>
  );
}

export function ViewTitle({ title, children }: { title: string; children: ReactNode }) {
  return (
    <>
      <h2 className="font-display text-[1.75rem] font-extrabold leading-tight text-ink [font-stretch:80%]">{title}</h2>
      <p className="mt-1 max-w-[66ch] text-[0.9375rem] text-pencil">{children}</p>
    </>
  );
}

/** The table twin of a chart: every value, no pointer needed. Scrolls inside itself, and keyboard users can scroll it too. */
export function TableFrame({ label, head, children }: { label: string; head: string[]; children: ReactNode }) {
  return (
    <div role="region" aria-label={label} tabIndex={0} className="mt-4 max-h-[26rem] overflow-auto rounded-md border border-rule">
      <table className="w-full border-collapse text-left text-[0.9375rem]">
        <thead className="sticky top-0 bg-check text-sm text-ink">
          <tr>
            {head.map((h, i) => (
              <th key={h} scope="col" className={`whitespace-nowrap px-3 py-2 font-bold ${i ? "text-right" : ""}`}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="[&_td]:px-3 [&_td]:py-2 [&_td]:text-right [&_th]:px-3 [&_th]:py-2 [&_th]:font-normal [&_tr]:border-t [&_tr]:border-rule">
          {children}
        </tbody>
      </table>
    </div>
  );
}
