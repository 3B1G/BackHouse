// Adapted from onboarding-06: a vertical progress timeline (done, in progress, upcoming). It takes its steps as props,
// adds "waiting" and "error" states and a list of checks under each step, and animates as steps arrive and finish:
// the connector fills in, the check mark pops, the running dot pulses. Colors come from the shadcn names in index.css.
import { IconAlertTriangle, IconCheck, IconCircleDashed, IconX } from "@tabler/icons-react";
import { AnimatePresence, motion } from "motion/react";
import { cn } from "@/lib/utils";

export type StepType = "done" | "in progress" | "open" | "waiting" | "error";
export type StepCheck = { status: "pass" | "warn" | "fail" | "pending"; text: string };
export type TimelineStep = {
  id: string;
  type: StepType;
  title: string;
  description?: string;
  activityTime: string;
  checks?: StepCheck[];
  muted?: boolean;
};

const CHECK_WORD = { pass: "Passed", warn: "Needs a look", fail: "Failed", pending: "Not yet" } as const;

function CheckIcon({ status }: { status: StepCheck["status"] }) {
  const c = "mt-0.5 size-4 shrink-0";
  if (status === "pass") return <IconCheck aria-hidden className={cn(c, "text-primary")} />;
  if (status === "warn") return <IconAlertTriangle aria-hidden className={cn(c, "text-serial")} />;
  if (status === "fail") return <IconX aria-hidden className={cn(c, "text-serial")} />;
  return <IconCircleDashed aria-hidden className={cn(c, "text-muted-foreground")} />;
}

function Marker({ type }: { type: StepType }) {
  if (type === "done")
    return (
      <motion.span initial={{ scale: 0.3, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} transition={{ type: "spring", stiffness: 520, damping: 22 }}>
        <IconCheck aria-hidden className="size-5 text-primary" />
      </motion.span>
    );
  if (type === "error") return <IconX aria-hidden className="size-5 text-serial" />;
  if (type === "in progress")
    return (
      <span className="relative flex size-2.5">
        <span aria-hidden className="absolute inline-flex size-full rounded-full bg-primary opacity-60 motion-safe:animate-ping" />
        <span aria-hidden className="relative inline-flex size-2.5 rounded-full bg-primary ring-4 ring-background" />
      </span>
    );
  if (type === "waiting")
    return <span aria-hidden className="size-3 rounded-full border-2 border-dashed border-serial bg-background ring-4 ring-background" />;
  return <span aria-hidden className="size-3 rounded-full border border-border bg-background ring-4 ring-background" />;
}

export function Onboarding06({
  title,
  steps,
  empty,
  className,
}: {
  title: string;
  steps: TimelineStep[];
  empty?: string;
  className?: string;
}) {
  return (
    <div className={cn("w-full", className)}>
      <h3 className="font-medium text-foreground">{title}</h3>
      {steps.length === 0 ? (
        <p className="mt-4 rounded-lg border-2 border-dashed border-border p-5 text-muted-foreground">{empty}</p>
      ) : (
        <ul className="mt-6 space-y-6">
          <AnimatePresence initial={false}>
            {steps.map((step, stepIdx) => (
              <motion.li
                key={step.id}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: step.muted ? 0.6 : 1, y: 0 }}
                transition={{ duration: 0.25, ease: "easeOut" }}
                className="relative flex gap-x-3"
              >
                <div className={cn("absolute top-0 left-0 flex w-6 justify-center", stepIdx === steps.length - 1 ? "h-6" : "-bottom-6")}>
                  <span aria-hidden className="w-px bg-border" />
                  {step.type === "done" && stepIdx < steps.length - 1 && (
                    <motion.span
                      aria-hidden
                      className="absolute top-0 left-[calc(50%-0.5px)] h-full w-px bg-primary"
                      style={{ originY: 0 }}
                      initial={{ scaleY: 0 }}
                      animate={{ scaleY: 1 }}
                      transition={{ duration: 0.45, ease: "easeOut" }}
                    />
                  )}
                </div>
                <div className="flex items-start space-x-2.5">
                  <div className="relative flex size-6 flex-none items-center justify-center bg-background">
                    <Marker type={step.type} />
                  </div>
                  <div className="min-w-0">
                    <p className="mt-0.5 text-sm font-medium text-foreground">
                      {step.title} <span className="ml-1 font-normal text-muted-foreground">{step.activityTime}</span>
                    </p>
                    {step.description && <p className="mt-0.5 text-sm leading-6 text-muted-foreground">{step.description}</p>}
                    {!!step.checks?.length && (
                      <ul className="mt-1.5 space-y-1">
                        {step.checks.map((c, i) => (
                          <motion.li
                            key={`${c.status}-${c.text}`}
                            initial={{ opacity: 0, x: -4 }}
                            animate={{ opacity: 1, x: 0 }}
                            transition={{ delay: i * 0.06, duration: 0.2 }}
                            className="flex gap-2 text-sm text-foreground"
                          >
                            <CheckIcon status={c.status} />
                            <span>
                              <span className="sr-only">{CHECK_WORD[c.status]}: </span>
                              {c.text}
                            </span>
                          </motion.li>
                        ))}
                      </ul>
                    )}
                  </div>
                </div>
              </motion.li>
            ))}
          </AnimatePresence>
        </ul>
      )}
    </div>
  );
}

export default Onboarding06;
