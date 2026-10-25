const SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const LONG = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

function parts(iso: string) {
  const [y, m, d] = iso.split("-").map(Number);
  return { y, m, d };
}

export const money = (cents: number) =>
  (cents / 100).toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

export const moneyExact = (cents: number) =>
  (cents / 100).toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2 });

/** "Feb 19" */
export const shortDate = (iso: string) => {
  const { m, d } = parts(iso);
  return `${SHORT[m - 1]} ${d}`;
};

/** "February 19" */
export const monthDay = (iso: string) => {
  const { m, d } = parts(iso);
  return `${LONG[m - 1]} ${d}`;
};

/** "June 25, 2027" */
export const fullDate = (iso: string) => {
  const { y, m, d } = parts(iso);
  return `${LONG[m - 1]} ${d}, ${y}`;
};

export const yearOf = (iso: string) => parts(iso).y;

/** "Feb 19, 2027" */
export const mediumDate = (iso: string) => {
  const { y, m, d } = parts(iso);
  return `${SHORT[m - 1]} ${d}, ${y}`;
};

/** "2027-01" -> "January 2027" */
export const monthYear = (ym: string) => `${LONG[+ym.slice(5, 7) - 1]} ${ym.slice(0, 4)}`;

/** "2027-01" -> "January" */
export const monthName = (ym: string) => LONG[+ym.slice(5, 7) - 1];

const MINUS = "−";

/** Money that can go below zero: "−$6,529", with a real minus sign. */
export const cash = (cents: number) => (Math.round(cents / 100) < 0 ? MINUS : "") + money(Math.abs(cents));

/** A change: "+$8,200" or "−$12,400". */
export const change = (cents: number) => (Math.round(cents / 100) < 0 ? MINUS : "+") + money(Math.abs(cents));

/** Axis labels: "$150K", "−$50K", "$0". */
export const compactMoney = (cents: number) => {
  const dollars = Math.abs(cents) / 100;
  const sign = cents < 0 ? MINUS : "";
  if (dollars >= 1_000_000) return `${sign}$${+(dollars / 1_000_000).toFixed(1)}M`;
  if (dollars >= 1_000) return `${sign}$${+(dollars / 1_000).toFixed(dollars >= 10_000 ? 0 : 1)}K`;
  return `${sign}$${Math.round(dollars)}`;
};

/** 11 -> "11 days", 42 -> "6 weeks" */
export const span = (days: number) => {
  const a = Math.abs(days);
  return a < 14 ? `${Math.round(a)} days` : `${Math.round(a / 7)} weeks`;
};

export const percent = (share: number) => `${Math.round(share * 100)}%`;

/** 0:07 */
export const clock = (seconds: number) => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
