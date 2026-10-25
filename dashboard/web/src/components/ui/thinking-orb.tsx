import { IconAlertTriangle, IconCheck } from "@tabler/icons-react";
import { useEffect, useRef, useState } from "react";

/* The dotted "thinking" orb of the ai-thinking-orb-and-input component (its canvas engine, light programs and easing),
 * restyled for Hermes. On paper, its dots work in the ballpoint blue of "a call going out" on the agent map, then sweep
 * to the check's ink, "an answer coming back", or to the serial red when the work failed. It assembles, thinks until
 * the work is over (and for at least minThinkMs, so a quick answer still reads), resolves, then condenses into a badge. */

type Rgb = readonly [number, number, number];
type Ease = (t: number) => number;
export type ThinkingOutcome = "thinking" | "done" | "failed";

const BALLPOINT: Rgb = [29, 111, 163]; // --color-ballpoint
const INK: Rgb = [31, 77, 54]; // --color-ink
const SERIAL: Rgb = [179, 38, 30]; // --color-serial
const TAU = Math.PI * 2;
const clamp01 = (v: number) => (v < 0 ? 0 : v > 1 ? 1 : v);
const lerp = (a: number, b: number, t: number) => a + (b - a) * t;

function mulberry32(seed: number) {
  let a = seed;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function cubicBezier(x1: number, y1: number, x2: number, y2: number): Ease {
  const cx = 3 * x1;
  const bx = 3 * (x2 - x1) - cx;
  const ax = 1 - cx - bx;
  const cy = 3 * y1;
  const by = 3 * (y2 - y1) - cy;
  const ay = 1 - cy - by;
  const sx = (t: number) => ((ax * t + bx) * t + cx) * t;
  const sy = (t: number) => ((ay * t + by) * t + cy) * t;
  const dx = (t: number) => (3 * ax * t + 2 * bx) * t + cx;
  const solve = (x: number) => {
    let t = x;
    for (let i = 0; i < 8; i++) {
      const e = sx(t) - x;
      if (Math.abs(e) < 1e-6) return t;
      const d = dx(t);
      if (Math.abs(d) < 1e-6) break;
      t -= e / d;
    }
    let lo = 0;
    let hi = 1;
    t = x;
    for (let i = 0; i < 40; i++) {
      const e = sx(t);
      if (Math.abs(e - x) < 1e-6) break;
      if (x > e) lo = t;
      else hi = t;
      t = (hi - lo) / 2 + lo;
    }
    return t;
  };
  return (x) => (x <= 0 ? 0 : x >= 1 ? 1 : sy(solve(x)));
}

const E = {
  out: cubicBezier(0.22, 1, 0.36, 1),
  io: cubicBezier(0.65, 0, 0.35, 1),
  vortex: cubicBezier(0.6, 0, 0.2, 1),
  spring: cubicBezier(0.34, 1.4, 0.64, 1),
};

// The sphere's dots, ring by ring from the top, each with a seed for its shimmer.
const RINGS = 16;
const DOTS = (() => {
  const rand = mulberry32(7);
  const out: { x: number; y: number; z: number; u: number; seed: number }[] = [];
  for (let k = 0; k < RINGS; k++) {
    const y = 1 - ((k + 0.5) / RINGS) * 2;
    const r = Math.sqrt(1 - y * y);
    const m = Math.max(4, Math.round(30 * r));
    for (let j = 0; j < m; j++) {
      const a = (j / m) * TAU + k * 0.35;
      out.push({ x: Math.cos(a) * r, y, z: Math.sin(a) * r, u: (1 - y) / 2, seed: rand() * 6.283 });
    }
  }
  return out;
})();
const N = DOTS.length;
const DX = Float32Array.from(DOTS, (d) => d.x);
const DY = Float32Array.from(DOTS, (d) => d.y);
const DZ = Float32Array.from(DOTS, (d) => d.z);
const DU = Float32Array.from(DOTS, (d) => d.u);
const DS = Float32Array.from(DOTS, (d) => d.seed);

// Fill styles from the working color to the done color (the sweep), each at every alpha step.
const G_STEPS = 24;
const A_STEPS = 48;
function palette(to: Rgb) {
  const out: string[] = [];
  for (let gi = 0; gi <= G_STEPS; gi++) {
    const g = gi / G_STEPS;
    const [r, gg, b] = [0, 1, 2].map((i) => Math.round(lerp(BALLPOINT[i], to[i], g)));
    for (let ai = 0; ai <= A_STEPS; ai++) out.push(`rgba(${r},${gg},${b},${(ai / A_STEPS).toFixed(3)})`);
  }
  return out;
}

type OrbParams = { k: number; alpha: number; spin: number; rot: number; sweep: number; pop: number; vortex: number; gain: number; floor: number; rad: number; prog: number };

function createOrb(canvas: HTMLCanvasElement, size: number, reduced: boolean) {
  const P: OrbParams = { k: 0, alpha: 0, spin: 0, rot: 0, sweep: 0, pop: 1, vortex: 0, gain: 1, floor: 0, rad: 0, prog: 0 };
  const ctx = canvas.getContext("2d");
  const lit = new Float32Array(N);
  const SX = new Float32Array(N);
  const SY = new Float32Array(N);
  const SR = new Float32Array(N);
  const SD = new Float32Array(N);
  const SC = new Int16Array(N);
  const pw = [1, 0, 0, 0];
  const R = size * 0.3;
  const C0 = size / 2;
  const S = 0.6;
  const CP = Math.cos(0.35);
  const SP = Math.sin(0.35);
  let colors = palette(INK);
  let time = reduced ? 1.2 : 0;
  let raf = 0;
  let last = 0;
  let dead = false;

  const dpr = Math.min(2, window.devicePixelRatio || 1);
  canvas.width = Math.round(size * dpr);
  canvas.height = Math.round(size * dpr);
  ctx?.setTransform(dpr, 0, 0, dpr, 0, 0);

  const draw = (c: CanvasRenderingContext2D, dt: number) => {
    c.clearRect(0, 0, size, size);
    time += dt;
    P.rot += P.spin * dt;
    const yaw = P.rot + P.vortex;
    const cyw = Math.cos(yaw);
    const syw = Math.sin(yaw);
    const stepW = dt / 0.35;
    for (let q = 0; q < 4; q++) {
      const d = (q === P.prog ? 1 : 0) - pw[q];
      pw[q] += Math.abs(d) <= stepW ? d : d > 0 ? stepW : -stepW;
    }
    const decay = Math.exp(-dt / 0.5);
    const h0 = (time * 300) % N;
    const h3 = (time * 480) % N;
    const a1 = time * 0.8;
    const b1 = Math.sin(time * 0.5) * 0.9;
    const f1x = Math.cos(b1) * Math.cos(a1);
    const f1y = Math.sin(b1);
    const f1z = Math.cos(b1) * Math.sin(a1);
    const a2 = time * 0.55 + 2.1;
    const b2 = Math.cos(time * 0.42) * 0.9;
    const f2x = Math.cos(b2) * Math.cos(a2);
    const f2y = Math.sin(b2);
    const f2z = Math.cos(b2) * Math.sin(a2);
    const lat = Math.sin(time * 2.2);
    const swirlK = P.vortex * 1.5;

    for (let n = 0; n < N; n++) {
      const dx = DX[n];
      const dy = DY[n];
      const dz = DZ[n];
      const u = DU[n];

      // The light programs light the dots in turn: a running bead, two roaming spots, a band, a faster bead.
      let pulse = 0;
      if (pw[0] > 0.001) {
        let dd = Math.abs(n - h0);
        if (dd > N - dd) dd = N - dd;
        const v = Math.max(0, 1 - dd / 16);
        pulse = Math.max(pulse, v * v * pw[0]);
      }
      if (pw[1] > 0.001) {
        const v1 = Math.max(0, (dx * f1x + dy * f1y + dz * f1z - 0.72) / 0.28);
        const v2 = Math.max(0, (dx * f2x + dy * f2y + dz * f2z - 0.72) / 0.28);
        const v = Math.max(v1, v2);
        pulse = Math.max(pulse, v * v * pw[1]);
      }
      if (pw[2] > 0.001) {
        const e = dy - lat;
        const v = Math.max(0, 1 - (e * e) / 0.02);
        pulse = Math.max(pulse, v * v * pw[2]);
      }
      if (pw[3] > 0.001) {
        let dd = Math.abs(n - h3);
        if (dd > N - dd) dd = N - dd;
        const v = Math.max(0, 1 - dd / 22);
        pulse = Math.max(pulse, v * v * pw[3]);
      }
      const l = Math.max(lit[n] * decay, pulse * P.gain);
      lit[n] = l;

      // Each dot appears top first; the bottom collapses first.
      const ki = clamp01(P.k * (1 + S) - S * u);
      if (ki <= 0.001) {
        SC[n] = -1;
        continue;
      }
      const eo = E.out(ki);
      const kk = eo * P.pop;
      const x1 = dx * cyw + dz * syw;
      const z1 = -dx * syw + dz * cyw;
      const y2 = dy * CP - z1 * SP;
      const z2 = dy * SP + z1 * CP;
      const f = 2.8 / (2.8 - z2);
      const depth = (z2 + 1) / 2;
      let ox = x1 * R * kk * f;
      let oy = -y2 * R * kk * f;
      if (swirlK > 0.001) {
        const sw = (1 - ki) * swirlK;
        const tx = ox * Math.cos(sw) - oy * Math.sin(sw);
        oy = ox * Math.sin(sw) + oy * Math.cos(sw);
        ox = tx;
      }

      const g = clamp01((P.sweep * 1.4 - u) / 0.4);
      let a = 0.1 + 0.035 * Math.sin(DS[n] + time * 1.6) * (1 - g) + 0.32 * depth * depth + 0.75 * l * (1 - g) + g * (0.55 + 0.4 * depth) + 2 * g * (1 - g);
      a = Math.max(a, P.floor * (0.7 + 0.3 * depth));
      a = Math.min(1, a) * eo * P.alpha;

      SX[n] = C0 + ox;
      SY[n] = C0 + oy;
      SD[n] = depth;
      SR[n] = (1.15 * (0.45 + 0.75 * depth) * f + 0.9 * l + g * 0.25) * (1 + P.rad) * (0.4 + 0.6 * eo);
      const ai = Math.round(a * A_STEPS);
      SC[n] = ai <= 0 ? -1 : Math.round(g * G_STEPS) * (A_STEPS + 1) + ai;
    }

    // The back half first, then the front.
    for (let pass = 0; pass < 2; pass++) {
      for (let n = 0; n < N; n++) {
        const col = SC[n];
        if (col < 0 || (SD[n] >= 0.5) !== (pass === 1)) continue;
        c.fillStyle = colors[col];
        c.beginPath();
        c.arc(SX[n], SY[n], SR[n], 0, TAU);
        c.fill();
      }
    }
  };

  const frame = (now: number) => {
    raf = 0;
    if (dead || !ctx) return;
    const dt = reduced ? 0 : Math.max(0, Math.min(0.05, (now - last) / 1000));
    last = now;
    draw(ctx, dt);
    if (P.alpha > 0.002) raf = requestAnimationFrame(frame);
    else ctx.clearRect(0, 0, size, size);
  };

  return {
    P,
    /** The color the dots sweep to as the work resolves. */
    sweepTo(to: Rgb) {
      colors = palette(to);
    },
    /** Keeps frames coming while the orb is visible. */
    ensure() {
      if (raf || dead || !ctx || P.alpha <= 0.002) return;
      last = performance.now();
      raf = requestAnimationFrame(frame);
    },
    destroy() {
      dead = true;
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
    },
  };
}

type Track = { set: (v: number) => void; from: number; to: number; t0: number; t1: number; ease: Ease };
const T = (set: (v: number) => void, from: number, to: number, t0: number, t1: number, ease: Ease = E.io): Track => ({ set, from, to, t0, t1, ease });

/** Plays the tracks on animation frames, `speed` times as fast; ends early once `alive` says no. */
function play(tracks: Track[], alive: () => boolean, speed: number) {
  return new Promise<void>((resolve) => {
    const end = Math.max(...tracks.map((k) => k.t1));
    let t = 0;
    let last = performance.now();
    const step = (now: number) => {
      if (!alive()) return resolve();
      t += Math.min(100, now - last) * speed;
      last = now;
      for (const k of tracks) {
        if (t >= k.t0) k.set(k.from + (k.to - k.from) * k.ease(Math.min(1, (t - k.t0) / Math.max(1, k.t1 - k.t0))));
      }
      if (t >= end) resolve();
      else requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });
}

const wait = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));

/** The orb for work under way: it thinks while `outcome` is "thinking", then shows how it went and calls onSettled.
 * Purely visual: the caller says in words what is happening. */
export function ThinkingOrb({
  outcome,
  labels,
  doneLabel,
  size = 168,
  minThinkMs = 2600,
  onSettled,
}: {
  outcome: ThinkingOutcome;
  /** What it shows while it works, in turn. */
  labels: string[];
  /** What it shows once the work is over. */
  doneLabel: string;
  size?: number;
  minThinkMs?: number;
  onSettled?: () => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const badgeRef = useRef<HTMLDivElement>(null);
  const ringRef = useRef<HTMLDivElement>(null);
  const latest = useRef({ outcome, labels, doneLabel, onSettled });
  latest.current = { outcome, labels, doneLabel, onSettled };
  const [label, setLabel] = useState({ text: labels[0] ?? "", n: 0 });
  const [over, setOver] = useState<"done" | "failed" | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const badge = badgeRef.current;
    const ring = ringRef.current;
    if (!canvas || !badge || !ring) return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const speed = reduced ? 8 : 1; // under reduced motion it settles at once and doesn't spin
    const orb = createOrb(canvas, size, reduced);
    const P = orb.P;
    let alive = true;
    const on = () => alive;

    void (async () => {
      // assemble
      await play(
        [
          T((v) => (P.k = v), 0, 1, 0, 800, E.out),
          T((v) => {
            P.alpha = v;
            orb.ensure();
          }, 0, 1, 0, 800, E.out),
          T((v) => (P.spin = v), 0, 0.9, 0, 800, E.out),
          T((v) => (P.pop = v), 1, 1.05, 0, 420, E.out),
          T((v) => (P.pop = v), 1.05, 1, 420, 800, E.io),
        ],
        on,
        speed,
      );
      // think, the labels in turn, until the work is over and the orb has thought long enough to read
      const started = performance.now();
      let i = 0;
      while (alive && (latest.current.outcome === "thinking" || performance.now() - started < minThinkMs)) {
        await wait(1150);
        if (!alive || (latest.current.outcome !== "thinking" && performance.now() - started >= minThinkMs)) break;
        const list = latest.current.labels;
        i = (i + 1) % Math.max(1, list.length);
        P.prog = i % 4;
        setLabel((l) => ({ text: list[i] ?? l.text, n: l.n + 1 }));
      }
      if (!alive) return;
      // resolve: the dots sweep to ink (or red), then condense into the badge
      const failed = latest.current.outcome === "failed";
      orb.sweepTo(failed ? SERIAL : INK);
      setOver(failed ? "failed" : "done");
      setLabel((l) => ({ text: latest.current.doneLabel, n: l.n + 1 }));
      await play(
        [
          T((v) => (P.sweep = v), 0, 1, 0, 700, E.io),
          T((v) => (P.spin = v), 0.9, 0.3, 0, 700, E.out),
          T((v) => (P.gain = v), 1, 0, 0, 700, E.out),
          T((v) => (P.floor = v), 0, 0.95, 400, 900, E.out),
          T((v) => (P.rad = v), 0, 0.15, 400, 900, E.out),
          T((v) => (P.pop = v), 1, 1.04, 600, 800, E.out),
          T((v) => (P.pop = v), 1.04, 1, 800, 900, E.out),
        ],
        on,
        speed,
      );
      await play(
        [
          T((v) => (P.pop = v), 1, 1.06, 0, 120, E.out),
          T((v) => (P.k = v), 1, 0, 120, 640, E.vortex),
          T((v) => (P.vortex = v), 0, 1.6, 120, 640, E.vortex),
          T((v) => (badge.style.opacity = String(v)), 0, 1, 260, 700, E.out),
          T((v) => (badge.style.transform = `translate(-50%, -50%) scale(${v})`), 0.55, 1, 260, 700, E.spring),
          T((v) => {
            ring.style.opacity = String(0.5 * (1 - v));
            ring.style.transform = `translate(-50%, -50%) scale(${1 + v})`;
          }, 0, 1, 320, 760, E.out),
          T((v) => (P.alpha = v), 1, 0, 500, 800, E.out),
        ],
        on,
        speed,
      );
      if (alive) latest.current.onSettled?.();
    })();

    return () => {
      alive = false;
      orb.destroy();
    };
  }, [size, minThinkMs]);

  const tone = over === "failed" ? "serial" : "ink";
  return (
    <div aria-hidden="true" className="flex flex-col items-center">
      <div className="relative" style={{ width: size, height: size }}>
        <canvas ref={canvasRef} style={{ width: size, height: size }} />
        <div
          ref={ringRef}
          className={`pointer-events-none absolute left-1/2 top-1/2 size-14 rounded-full border-2 ${tone === "serial" ? "border-serial" : "border-ink"}`}
          style={{ opacity: 0, transform: "translate(-50%, -50%)" }}
        />
        <div
          ref={badgeRef}
          className={`absolute left-1/2 top-1/2 grid size-14 place-items-center rounded-full text-paper shadow-[0_10px_24px_-12px_rgba(31,77,54,0.6)] ${tone === "serial" ? "bg-serial" : "bg-ink"}`}
          style={{ opacity: 0, transform: "translate(-50%, -50%) scale(0.55)" }}
        >
          {over === "failed" ? <IconAlertTriangle size={26} /> : <IconCheck size={28} stroke={2.5} />}
        </div>
      </div>
      <p className="mt-1 flex min-h-7 items-center gap-1.5 text-[0.9375rem] font-bold text-ink">
        <span key={label.n} className="animate-[thinking-in_0.3s_ease-out]">
          {label.text}
        </span>
        {!over && (
          <span className="flex gap-1">
            {[0, 1, 2].map((d) => (
              <span key={d} className="size-1 animate-[thinking-dot_1.2s_ease-in-out_infinite] rounded-full bg-ink" style={{ animationDelay: `${d * 0.16}s` }} />
            ))}
          </span>
        )}
      </p>
    </div>
  );
}
