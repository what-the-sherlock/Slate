import { cn } from "@/lib/utils";
import { Check } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";
import { Logo, LogoTile } from "@/components/brand";
import { delay, REVEAL } from "@/lib/reveal";
import { HowItWorks } from "./how-it-works";

/**
 * The sign-in / setup frame (docs/UI_DESIGN.md §4.1, ADR-0017), after Seekworthy's onboarding
 * hero. It always fits one screen: the root is exactly the viewport tall, spacing scales with
 * viewport height (clamp), spare height is split 2:3 above and below the hero, the subtitle hides below 680 px tall and the card below 640 px.
 */
export function AuthFrame({
  headline,
  subtitle,
  aside,
  children,
}: {
  headline: ReactNode;
  subtitle: ReactNode;
  /** Right side of the top bar. Defaults to the GitHub link. */
  aside?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="flex h-svh flex-col overflow-hidden bg-bg">
      <header className="flex h-14 shrink-0 items-center justify-between px-5 sm:h-16 lg:px-10">
        <Logo size={28} />
        <div className="flex items-center gap-1">
          <Link
            href="/privacy"
            className="inline-flex h-8 items-center rounded-md px-3 text-body text-fg-muted transition-colors hover:bg-muted hover:text-fg"
          >
            Privacy
          </Link>
          {aside ?? (
            <a
              href="https://github.com/what-the-sherlock/Slate"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex h-8 items-center gap-2 rounded-md px-3 text-body text-fg-muted transition-colors hover:bg-muted hover:text-fg"
            >
              <LogoTile connector="github" size={18} />
              <span className="hidden sm:inline">Source on GitHub</span>
              <span aria-hidden="true">↗</span>
            </a>
          )}
        </div>
      </header>

      <GrainPanel className="mx-3 mb-3 flex min-h-0 flex-1 flex-col items-center rounded-[24px] px-4 pt-[clamp(16px,4vh,48px)] sm:mx-5 sm:mb-5 sm:rounded-[28px]">
        <div className="min-h-0 flex-[2]" />
        <main
          id="main"
          className="relative flex w-full shrink-0 flex-col items-center text-center"
        >
          <h1
            className={cn(
              "text-[clamp(28px,4.6vh,50px)] leading-[1.05] font-semibold tracking-[-0.03em] text-balance text-white",
              "[text-shadow:0_2px_24px_rgb(16_24_90/0.25)]",
              REVEAL,
            )}
          >
            {headline}
          </h1>
          <p
            className={cn(
              "mt-[clamp(8px,1.6vh,16px)] max-w-[520px] text-[15px] leading-relaxed text-pretty text-white sm:text-[16px] [@media(max-height:680px)]:hidden",
              REVEAL,
            )}
            style={delay(80)}
          >
            {subtitle}
          </p>
          <div
            className={cn(
              "mt-[clamp(16px,3.2vh,36px)] w-full max-w-[600px]",
              REVEAL,
            )}
            style={delay(160)}
          >
            {children}
          </div>
        </main>
        <div className="min-h-4 flex-[3]" />
        <div className="flex w-full shrink-0 justify-center [@media(max-height:640px)]:hidden">
          <HowItWorks />
        </div>
      </GrainPanel>
    </div>
  );
}

/**
 * Gradient + grain (tokens in styles/tokens.css). White text never sits below the mid stop
 * (white on mid 5.05:1). Overlay grain is ~2.5x weaker on dark blue than on light, so a second
 * layer of the same tile fades out top to bottom to make the texture read evenly.
 */
function GrainPanel({
  className,
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  return (
    <div className={cn("relative overflow-hidden", className)}>
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "radial-gradient(90% 60% at 15% 0%, rgb(255 255 255 / 0.08) 0%, transparent 55%), linear-gradient(180deg, var(--sl-hero-deep) 0%, var(--sl-hero-mid) 55%, var(--sl-hero-light) 100%)",
        }}
      />
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 bg-[url(/brand/noise.png)] bg-size-[160px_160px] bg-repeat opacity-(--sl-grain-base) mix-blend-overlay"
      />
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 bg-[url(/brand/noise.png)] bg-size-[160px_160px] bg-position-[80px_80px] bg-repeat opacity-(--sl-grain-boost) mix-blend-overlay [mask-image:linear-gradient(180deg,#000_0%,transparent_75%)]"
      />
      {children}
    </div>
  );
}

/** White check-mark reassurances under the form. */
export function TrustRow({ items }: { items: string[] }) {
  return (
    <ul className="flex flex-wrap items-center justify-center gap-x-6 gap-y-2">
      {items.map((t) => (
        <li key={t} className="flex items-center gap-1.5 text-body text-white">
          <Check className="size-3.5" strokeWidth={2.5} aria-hidden="true" />
          {t}
        </li>
      ))}
    </ul>
  );
}

/** The white floating surface shared by the sign-in bar and the setup card. */
export const heroSurface =
  "bg-bg shadow-[0_1px_2px_rgb(16_24_90/0.1),0_20px_48px_-20px_rgb(16_24_90/0.55)] ring-1 ring-white/80";

/** Field pieces used inside the setup card. */
export const fieldShell =
  "flex items-center gap-2.5 rounded-md border border-(--sl-border-input) bg-bg px-3 transition-[border-color,box-shadow] focus-within:border-brand focus-within:shadow-[0_0_0_3px_var(--sl-accent-subtle)] has-[[aria-invalid=true]]:border-danger";
export const fieldIcon = "size-4 shrink-0 text-fg-subtle";
export const fieldInput =
  "h-[38px] min-w-0 flex-1 bg-transparent text-base text-fg outline-none placeholder:text-fg-subtle focus-visible:outline-none sm:text-caption";
export const fieldLabel = "text-caption font-medium text-fg";
