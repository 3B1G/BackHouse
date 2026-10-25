// Adapted from "how-it-works": cards pinned to ruled paper, zig-zagging down the page, joined by a moving dashed line.
// Our version: each step is a guest check clipped to the kitchen's ticket rail, numbered in red like a real
// check, with Agent and Uses boxes, and a rubber stamp that lands as the step finishes. The line is drawn from where
// the checks actually sit, so it follows any text length; below 1024px the checks stack and the line hides.
import { LazyMotion, domAnimation, m, useReducedMotion } from "motion/react";
import { useLayoutEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";

export type Stamp = { tone: "done" | "working" | "waiting" | "next" | "ready"; text: string };
export type TicketStep = {
  title: string;
  description: string;
  agent: string;
  uses: string;
  status?: string;
  stamp?: Stamp;
  href?: string;
};

const STAMP = {
  done: "border-ink text-ink",
  working: "border-ballpoint text-ballpoint",
  waiting: "border-serial text-serial",
  next: "border-pencil text-pencil",
  ready: "border-ink text-ink",
} as const;

/** The clip that holds a check on the rail. */
function Clip() {
  return (
    <svg aria-hidden="true" viewBox="0 0 64 30" className="absolute -top-4 left-1/2 z-20 h-8 w-16 -translate-x-1/2">
      <path d="M18 13V6a4 4 0 0 1 4-4h20a4 4 0 0 1 4 4v7" fill="none" stroke="var(--color-pencil)" strokeWidth="2.5" />
      <rect x="6" y="12" width="52" height="16" rx="3.5" fill="var(--color-ink)" />
      <rect x="6" y="12" width="52" height="5" rx="2.5" fill="#2f6b4c" />
    </svg>
  );
}

function Ticket({
  step,
  number,
  side,
  first,
  cardRef,
}: {
  step: TicketStep;
  number: number;
  side: "left" | "right";
  first: boolean;
  cardRef: (el: HTMLElement | null) => void;
}) {
  const reduce = useReducedMotion();
  const tilt = side === "left" ? -2 : 2;
  return (
    <m.article
      ref={cardRef}
      initial={reduce ? false : { opacity: 0, y: -26, rotate: tilt * 3 }}
      whileInView={{ opacity: 1, y: 0, rotate: tilt }}
      viewport={{ once: true, amount: 0.25 }}
      transition={{ type: "spring", stiffness: 160, damping: 13 }}
      whileHover={reduce ? undefined : { scale: 1.03, rotate: 0, zIndex: 30 }}
      className={cn(
        "relative z-10 w-full max-w-[26rem] lg:w-[23rem]",
        side === "left" ? "lg:ml-[3%] lg:mr-auto" : "lg:ml-auto lg:mr-[3%]",
        !first && "lg:-mt-16",
      )}
    >
      <Clip />
      <div className="stub relative overflow-hidden rounded-md border-2 border-rule bg-check shadow-[0_22px_30px_-20px_rgba(31,77,54,0.55)]">
        <div className="flex items-start justify-between gap-4 border-b-2 border-rule bg-paper/60 px-4 pb-2 pt-5">
          <p className="font-display text-[1.9rem] font-extrabold leading-none text-serial [font-stretch:66%]">
            <span className="sr-only">Step </span>No. {String(number).padStart(2, "0")}
          </p>
          <dl className="grid grid-cols-[auto_auto] items-baseline gap-x-2 text-right text-xs leading-5">
            <dt className="text-pencil">Agent</dt>
            <dd className="font-bold text-ink">{step.agent}</dd>
            <dt className="text-pencil">Uses</dt>
            <dd className="text-carbon">{step.uses}</dd>
          </dl>
        </div>
        <div
          className={cn("relative px-4 pt-2", step.stamp ? "pb-16" : "pb-5")}
          style={{ backgroundImage: "repeating-linear-gradient(to bottom, transparent 0 1.6875rem, #d3e3ca 1.6875rem 1.75rem)" }}
        >
          <h3 className="font-display text-[1.6rem] font-extrabold leading-[1.75rem] text-ink [font-stretch:80%]">
            {step.href ? (
              <a href={step.href} className="underline decoration-2 underline-offset-4">
                {step.title}
              </a>
            ) : (
              step.title
            )}
          </h3>
          <p className="mt-[1.75rem] text-[0.9375rem] leading-[1.75rem] text-carbon">{step.description}</p>
          {step.status && <p className="text-sm font-bold leading-[1.75rem] text-ink">{step.status}</p>}
          {step.stamp && (
            <m.span
              key={step.stamp.text}
              initial={reduce ? false : { scale: 1.9, opacity: 0, rotate: -20 }}
              animate={{ scale: 1, opacity: 0.88, rotate: -9 }}
              transition={{ type: "spring", stiffness: 420, damping: 18 }}
              className={cn(
                "absolute bottom-3 right-4 rounded-md border-[3px] px-2.5 py-0.5 font-display text-xl font-extrabold [font-stretch:80%]",
                STAMP[step.stamp.tone],
              )}
            >
              {step.stamp.text}
            </m.span>
          )}
        </div>
      </div>
    </m.article>
  );
}

export default function HowItWorks({ steps, start = 1, className }: { steps: TicketStep[]; start?: number; className?: string }) {
  const box = useRef<HTMLOListElement>(null);
  const cards = useRef<(HTMLElement | null)[]>([]);
  const [line, setLine] = useState<{ w: number; h: number; d: string } | null>(null);
  const reduce = useReducedMotion();

  // The dashed line runs from each check's inner edge to the next one's, wherever the layout put them. It is redrawn
  // whenever the steps change (order, status, stamp), any check changes size, the window resizes or the fonts arrive.
  const order = steps.map((s) => `${s.title}|${s.status ?? ""}|${s.stamp?.text ?? ""}`).join("/");
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    let alive = true;
    const measure = () => {
      if (!alive) return;
      if (!window.matchMedia("(min-width: 1024px)").matches || steps.length < 2) return setLine(null);
      const pts = cards.current.slice(0, steps.length).map((c, i) => {
        if (!c) return [0, 0];
        return [i % 2 === 0 ? c.offsetLeft + c.offsetWidth : c.offsetLeft, c.offsetTop + c.offsetHeight / 2];
      });
      let d = `M${pts[0][0]} ${pts[0][1]}`;
      for (let i = 1; i < pts.length; i++) {
        const [px, py] = pts[i - 1];
        const [x, y] = pts[i];
        const dir = Math.sign(x - px) || 1;
        d += ` C${px + dir * 90} ${py} ${x - dir * 90} ${y} ${x} ${y}`;
      }
      setLine({ w: el.offsetWidth, h: el.offsetHeight, d });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    for (const c of cards.current.slice(0, steps.length)) if (c) observer.observe(c);
    window.addEventListener("resize", measure); // a narrower window can leave this box's size unchanged
    document.fonts?.ready.then(measure).catch(() => {});
    return () => {
      alive = false;
      observer.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [order, steps.length]);

  return (
    <LazyMotion features={domAnimation}>
      <div className={cn("relative px-4 py-10 lg:py-14", className)}>
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 opacity-70"
          style={{ backgroundImage: "linear-gradient(var(--color-check-deep) 1px, transparent 1px)", backgroundSize: "100% 2rem" }}
        />
        <div aria-hidden="true" className="pointer-events-none absolute inset-y-0 left-0 w-1/3" style={{ background: "linear-gradient(to right, var(--color-paper), transparent)" }} />
        <div aria-hidden="true" className="pointer-events-none absolute inset-y-0 right-0 w-1/3" style={{ background: "linear-gradient(to left, var(--color-paper), transparent)" }} />
        <ol ref={box} className="relative mx-auto flex max-w-[64rem] flex-col items-center gap-12 lg:block">
          {line && (
            <svg aria-hidden="true" className="pointer-events-none absolute left-0 top-0 z-0" width={line.w} height={line.h} viewBox={`0 0 ${line.w} ${line.h}`}>
              <m.path
                d={line.d}
                fill="none"
                stroke="var(--color-ink)"
                strokeOpacity={0.45}
                strokeWidth={2}
                strokeDasharray="8 6"
                strokeLinecap="round"
                initial={{ strokeDashoffset: 0 }}
                animate={reduce ? undefined : { strokeDashoffset: -140 }}
                transition={{ duration: 3, repeat: Infinity, ease: "linear" }}
              />
            </svg>
          )}
          {steps.map((step, i) => (
            <li key={step.title} className="contents">
              <Ticket
                step={step}
                number={start + i}
                side={i % 2 === 0 ? "left" : "right"}
                first={i === 0}
                cardRef={(c) => {
                  cards.current[i] = c;
                }}
              />
            </li>
          ))}
        </ol>
      </div>
    </LazyMotion>
  );
}
