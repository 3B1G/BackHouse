import type { Forecast, SpendRow } from "../../api";
import { money, monthName, percent } from "../../format";
import { TableFrame, ViewTitle } from "./ChartBits";

/** "Protein $14,501, dairy $12,160, ..." */
const partsText = (r: SpendRow) => {
  const text = r.parts.map((p) => `${p.label} ${money(p.cents)}`).join(", ");
  return text.charAt(0).toUpperCase() + text.slice(1);
};

/** Every value is written beside its bar, so this chart needs no hover. */
export function SpendChart({ f, table }: { f: Forecast; table: boolean }) {
  const s = f.history.spending;
  const max = Math.max(...s.rows.map((r) => r.cents));
  return (
    <figure className="m-0">
      <ViewTitle title="Where your money goes">
        In an average month from {monthName(s.from)} to {monthName(s.to)}, {money(s.in_cents)} came in and {money(s.out_cents)} went
        out.
      </ViewTitle>
      {table ? (
        <TableFrame label="Spending by kind, as a table" head={["Spent on", "A month", "Share"]}>
          {s.rows.map((r) => (
            <tr key={r.label}>
              <th scope="row">
                {r.label}
                {r.parts.length > 0 && <span className="block text-sm text-pencil">{partsText(r)}</span>}
              </th>
              <td>{money(r.cents)}</td>
              <td>{percent(r.cents / s.out_cents)}</td>
            </tr>
          ))}
        </TableFrame>
      ) : (
        <ul className="mt-5 space-y-3">
          {s.rows.map((r) => (
            <li key={r.label} className="grid grid-cols-[5.75rem_minmax(0,1fr)] items-center gap-x-3 sm:grid-cols-[7rem_minmax(0,1fr)]">
              <span className="text-[0.9375rem] font-bold text-carbon">{r.label}</span>
              <span className="flex min-w-0 items-center gap-2">
                <span
                  aria-hidden="true"
                  className="h-3.5 min-w-[3px] rounded-r-[4px] bg-ballpoint"
                  style={{ width: `calc((100% - 7.5rem) * ${(r.cents / max).toFixed(4)})` }}
                />
                <span className="whitespace-nowrap text-[0.9375rem] text-carbon">
                  {money(r.cents)}
                  <span className="ml-2 text-pencil">{percent(r.cents / s.out_cents)}</span>
                </span>
              </span>
              {r.parts.length > 0 && <span className="col-start-2 mt-0.5 text-sm text-pencil">{partsText(r)}</span>}
            </li>
          ))}
        </ul>
      )}
    </figure>
  );
}
