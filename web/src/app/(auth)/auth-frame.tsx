import { Check } from "lucide-react";
import type { ReactNode } from "react";
import { Logo, LogoTile } from "@/components/brand";

/**
 * The sign-in / setup frame (docs/UI_DESIGN.md §4.1): a soft blue mesh-gradient panel
 * with fine grain, a display headline, and a "How it works" card overlapping its foot.
 */
const GRAIN =
  "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E\")";

export function AuthFrame({
  headline,
  subtitle,
  children,
}: {
  headline: ReactNode;
  subtitle: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="flex min-h-svh flex-col bg-bg">
      <header className="flex h-16 items-center justify-between px-4 sm:px-8">
        <Logo />
        <a
          href="https://github.com/what-the-sherlock/Slate"
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex h-9 items-center gap-2 rounded-md px-3 text-sm font-medium text-fg-muted transition-colors hover:bg-muted hover:text-fg"
        >
          <LogoTile connector="github" size={20} />
          Source on GitHub
          <span aria-hidden="true">↗</span>
        </a>
      </header>

      <main
        id="main"
        className="relative mx-3 mb-6 flex min-h-[calc(100svh-88px)] flex-col items-center overflow-hidden rounded-[20px] px-4 pt-16 pb-0 sm:mx-6 sm:rounded-xl sm:pt-24"
        style={{
          background:
            "radial-gradient(60% 55% at 15% 20%, #e8ecff 0%, transparent 70%), radial-gradient(55% 60% at 85% 30%, #c9d2ff 0%, transparent 70%), radial-gradient(50% 50% at 50% 100%, #8ea0ff 0%, transparent 75%), radial-gradient(35% 35% at 95% 95%, rgb(48 80 240 / 0.35) 0%, transparent 70%), #f4f6ff",
        }}
      >
        <div aria-hidden="true" className="pointer-events-none absolute inset-0 opacity-[0.07]" style={{ backgroundImage: GRAIN }} />
        <div className="relative z-10 flex w-full max-w-[640px] flex-col items-center text-center">
          <h1 className="text-[36px] leading-[40px] font-medium tracking-[-0.04em] text-[#1a2150] sm:text-[52px] sm:leading-[56px]">
            {headline}
          </h1>
          <p className="mt-4 max-w-[560px] text-[18px] leading-7 tracking-[-0.02em] text-fg-body">{subtitle}</p>
          <div className="mt-9 w-full max-w-[560px]">{children}</div>
          <ul className="mt-5 flex flex-wrap justify-center gap-x-6 gap-y-2 text-sm text-fg-muted">
            {["Cited answers", "Permission-aware", "Read-only demo"].map((item) => (
              <li key={item} className="inline-flex items-center gap-1.5">
                <Check className="size-3.5 text-brand-strong" aria-hidden="true" />
                {item}
              </li>
            ))}
          </ul>
        </div>
        <HowItWorks />
      </main>
    </div>
  );
}

function HowItWorks() {
  const steps = [
    {
      title: "Connect sources",
      body: "Upload files today; GitHub, Discord and Gmail next.",
      art: (
        <div className="flex gap-2" aria-hidden="true">
          {(["upload", "github", "discord", "gmail"] as const).map((c) => (
            <LogoTile key={c} connector={c} size={32} />
          ))}
        </div>
      ),
    },
    {
      title: "Ask a question",
      body: "Slate searches only what you're allowed to see.",
      art: (
        <div aria-hidden="true" className="flex h-9 items-center gap-2 rounded-md border border-border bg-subtle px-3 text-[13px] text-fg-subtle">
          <span className="inline-block size-2 animate-pulse rounded-full bg-brand motion-reduce:animate-none" />
          Searching 4 sources…
        </div>
      ),
    },
    {
      title: "Check the answer",
      body: "Every claim links to the passage it came from.",
      art: (
        <p aria-hidden="true" className="text-[13px] leading-5 text-fg-body">
          Leave is 25 days a year
          <span className="mx-0.5 inline-flex h-4 min-w-4 items-center justify-center rounded-xs bg-brand px-1 align-[1px] text-[11px] font-semibold text-white">
            1
          </span>
        </p>
      ),
    },
  ];
  return (
    <section
      aria-labelledby="how"
      className="relative z-10 mt-auto w-full max-w-[1120px] translate-y-px rounded-t-lg border border-b-0 border-border bg-bg"
    >
      <div className="flex h-12 items-center justify-between border-b border-border px-6">
        <h2 id="how" className="text-sm font-medium text-fg">
          How it works
        </h2>
        <span className="hidden text-[13px] text-fg-subtle sm:inline">Built on free-tier infrastructure</span>
      </div>
      <ol className="grid divide-y divide-border sm:grid-cols-3 sm:divide-x sm:divide-y-0">
        {steps.map((step, i) => (
          <li key={step.title} className="flex flex-col gap-3 p-6">
            <h3 className="text-[15px] font-medium text-fg">
              <span className="mr-2 text-brand-strong tabular-nums">{i + 1}</span>
              {step.title}
            </h3>
            {step.art}
            <p className="text-[13px] text-fg-subtle">{step.body}</p>
          </li>
        ))}
      </ol>
    </section>
  );
}
