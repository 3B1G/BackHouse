import { useEffect, useLayoutEffect, useRef, useState } from "react";
import NavMenu from "@/components/ui/menu-hover-effects";
import type { Route } from "../App";
import { LINKS } from "../links";

export function CheckMark({ className = "" }: { className?: string }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 32 32" className={className}>
      <path
        d="M7 3h18v23l-3-2.5-3 2.5-3-2.5-3 2.5-3-2.5-3 2.5z"
        fill="var(--color-check)"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinejoin="round"
      />
      <path d="M11 10h10M11 14h10M11 18h6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
      <circle cx="22" cy="7.5" r="1.6" fill="var(--color-serial)" />
    </svg>
  );
}

const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const two = (n: number) => String(n).padStart(2, "0");

/** "October 24, 19:42:01", ticking on the second. Not announced to screen readers every second. */
function Clock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    let timer = 0;
    const tick = () => {
      const d = new Date();
      setNow(d);
      timer = window.setTimeout(tick, 1010 - d.getMilliseconds());
    };
    timer = window.setTimeout(tick, 1010 - new Date().getMilliseconds());
    return () => window.clearTimeout(timer);
  }, []);
  return (
    <time dateTime={now.toISOString()} className="whitespace-nowrap text-[0.9375rem] font-bold tabular-nums text-paper sm:text-lg">
      {MONTHS[now.getMonth()]} {now.getDate()}, {two(now.getHours())}:{two(now.getMinutes())}:{two(now.getSeconds())}
    </time>
  );
}

function MenuIcon({ open }: { open: boolean }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 20 20" className="h-6 w-6" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      {open ? <path d="M5 5l10 10M15 5L5 15" /> : <path d="M3 5.5h14M3 10h14M3 14.5h14" />}
    </svg>
  );
}

/** Two bars. The top one (name and restaurant) slides away while the owner scrolls down and comes back when they scroll
 * up; the dark bar under it, with the date, time and menu, always stays. */
export function Header({ route, restaurant, onNewCheck }: { route: Route; restaurant: string | null; onNewCheck: () => void }) {
  const header = useRef<HTMLElement>(null);
  const top = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ top: 0, all: 0 });
  const [hidden, setHidden] = useState(false);
  const [open, setOpen] = useState(false);
  const tucked = hidden; // the menu lives on the bar that stays, so opening it never needs the top bar back

  // The bars grow with the page's text size, so measure them.
  useLayoutEffect(() => {
    const measure = () => setSize({ top: top.current?.offsetHeight ?? 0, all: header.current?.offsetHeight ?? 0 });
    measure();
    const observer = new ResizeObserver(measure);
    if (header.current) observer.observe(header.current);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    let last = window.scrollY;
    const onScroll = () => {
      const y = window.scrollY;
      if (y <= size.top) setHidden(false);
      else if (y > last + 6) setHidden(true);
      else if (y < last - 6) setHidden(false);
      last = y;
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [size.top]);

  // Sticky things below (the agents panel) sit just under whatever part of the header is showing.
  useEffect(() => {
    document.documentElement.style.setProperty("--header-visible", `${tucked ? size.all - size.top : size.all}px`);
  }, [tucked, size]);

  useEffect(() => setOpen(false), [route]);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setOpen(false);
      button.current?.focus();
    };
    const onDown = (e: PointerEvent) => {
      const target = e.target as Node;
      if (!panel.current?.contains(target) && !button.current?.contains(target)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onDown);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onDown);
    };
  }, [open]);

  return (
    <header
      ref={header}
      className="sticky top-0 z-40 transition-transform duration-200 ease-out"
      style={{ transform: tucked ? `translateY(-${size.top}px)` : undefined }}
    >
      <div ref={top} className="border-b border-rule bg-paper" onFocus={() => setHidden(false)}>
        <div className="flex min-h-14 items-center justify-between gap-6 px-4 py-2 sm:px-6 lg:px-8">
          <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-0.5">
          <a
            href="#/"
            className="flex min-h-11 items-center gap-2 font-display text-xl font-extrabold text-ink no-underline [font-stretch:80%]"
          >
            <CheckMark className="h-7 w-7 text-ink" />
            Hermes
          </a>
          <span className="text-[0.9375rem] text-pencil">{restaurant ?? "No restaurant connected yet"}</span>
          </div>
          <NavMenu className="hidden shrink-0 lg:block" items={LINKS.map((l) => ({ label: l.label, href: l.href, current: l.route === route }))} />
        </div>
      </div>
      <div className="bg-ink">
        <div className="flex min-h-14 items-center justify-between gap-4 px-4 sm:px-6 lg:px-8">
          <Clock />
          <div className="relative">
            <button
              ref={button}
              type="button"
              aria-expanded={open}
              aria-controls="site-menu"
              onClick={() => setOpen((o) => !o)}
              className={`-mr-2 inline-flex min-h-11 items-center gap-2.5 rounded-md pl-3 pr-2.5 text-paper transition-colors duration-150 hover:bg-white/10 focus-visible:outline-paper ${
                open ? "bg-white/15" : ""
              }`}
            >
              {/* The page you're on, so the bar that never scrolls away always says where you are */}
              <span className="whitespace-nowrap text-[0.9375rem] font-bold sm:text-[1.0625rem]">{LINKS.find((l) => l.route === route)?.label}</span>
              <span className="sr-only"> menu</span>
              <MenuIcon open={open} />
            </button>
            {open && (
              <div
                ref={panel}
                id="site-menu"
                className="absolute right-0 top-full mt-1.5 w-[min(22rem,calc(100vw-2rem))] rounded-lg border-2 border-rule bg-paper p-2 shadow-[0_16px_32px_-16px_rgba(31,77,54,0.45)]"
              >
                <nav aria-label="Main">
                  <ul>
                    {LINKS.map((l) => {
                      const here = l.route === route;
                      return (
                        <li key={l.route}>
                          <a
                            href={l.href}
                            aria-current={here ? "page" : undefined}
                            onClick={() => setOpen(false)}
                            className={`flex min-h-12 flex-col justify-center rounded-md px-3 py-2 no-underline transition-colors duration-150 ${
                              here ? "bg-check" : "hover:bg-check"
                            }`}
                          >
                            <span className="font-bold text-ink">{l.label}</span>
                            <span className="text-sm text-pencil">{l.detail}</span>
                          </a>
                        </li>
                      );
                    })}
                  </ul>
                </nav>
                <div className="mt-1 border-t border-rule pt-1">
                  <button
                    type="button"
                    onClick={() => {
                      setOpen(false);
                      onNewCheck();
                    }}
                    className="flex min-h-12 w-full flex-col justify-center rounded-md px-3 py-2 text-left transition-colors duration-150 hover:bg-check"
                  >
                    <span className="font-bold text-ink">Start a new check</span>
                    <span className="text-sm text-pencil">Connect the bank and add records again</span>
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </header>
  );
}
