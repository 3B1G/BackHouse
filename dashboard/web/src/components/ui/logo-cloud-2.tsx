// Adapted from "logo-cloud-2": logos in a ruled grid whose top and bottom lines run out to the page edges, with a plus
// mark wherever its lines meet. BackHouse's version takes the logos as props, each with a heading and a line on what it
// does here, and fits any count: stacked on phones, one row from 768px. The plus is Tabler's, the app's icon set, and is
// sized in px so it stays centered on the 1px lines at any page text size.
import { IconPlus } from "@tabler/icons-react";
import type { ComponentProps } from "react";
import { cn } from "@/lib/utils";

export type Logo = {
  src: string;
  alt: string;
  width?: number;
  height?: number;
  /** Sizes this logo, so logos of different shapes look about as big as each other. */
  className?: string;
  name?: string;
  detail?: string;
};

type LogoCloudProps = ComponentProps<"div"> & { logos: Logo[] };

export function LogoCloud({ logos, className, ...props }: LogoCloudProps) {
  return (
    <div className={cn("relative grid grid-cols-1 border-x border-border md:auto-cols-fr md:grid-flow-col", className)} {...props}>
      <div className="pointer-events-none absolute -top-px left-1/2 w-screen -translate-x-1/2 border-t border-border" />

      {logos.map((logo, i) => (
        <LogoCard
          key={logo.alt}
          logo={logo}
          className={cn(i % 2 === 1 && "bg-secondary", i > 0 && "border-t border-border md:border-t-0 md:border-l")}
        >
          {/* This card's dividing line meets the outer lines at both its ends: left and right on phones, top and bottom from 768px */}
          {i > 0 && (
            <>
              <Plus className="-left-[12.5px] -top-[12.5px]" />
              <Plus className="-right-[12.5px] -top-[12.5px] md:hidden" />
              <Plus className="-bottom-[12.5px] -left-[12.5px] hidden md:block" />
            </>
          )}
        </LogoCard>
      ))}

      <div className="pointer-events-none absolute -bottom-px left-1/2 w-screen -translate-x-1/2 border-b border-border" />
    </div>
  );
}

function Plus({ className }: { className: string }) {
  return <IconPlus aria-hidden="true" size={24} stroke={1} className={cn("absolute z-10 text-ink", className)} />;
}

type LogoCardProps = ComponentProps<"div"> & {
  logo: Logo;
};

function LogoCard({ logo, className, children, ...props }: LogoCardProps) {
  return (
    <div className={cn("relative flex flex-col items-center bg-background px-6 py-10 text-center md:px-8", className)} {...props}>
      <div className="flex h-12 items-center">
        <img
          alt={logo.alt}
          className={cn("pointer-events-none h-8 w-auto select-none", logo.className)}
          height={logo.height}
          src={logo.src}
          width={logo.width}
        />
      </div>
      {logo.name && <h3 className="mt-5 font-display text-xl font-extrabold text-ink [font-stretch:80%]">{logo.name}</h3>}
      {logo.detail && <p className="mt-1 max-w-[34ch] text-[0.9375rem] text-pencil">{logo.detail}</p>}
      {children}
    </div>
  );
}
