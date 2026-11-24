import { cn } from "cn";
import { MessageSquare, Upload } from "lucide-react";
import {
  siConfluence,
  siDiscord,
  siGithub,
  siGmail,
  siGoogledrive,
  siNotion,
  type SimpleIcon,
} from "simple-icons";

/**
 * Brand marks from Simple Icons (CC0), drawn in each brand's colour (UI_DESIGN §2.6).
 * Trademarks belong to their owners and are used only to name the integration.
 * Slack's mark was withdrawn from Simple Icons at Slack's request, so its "coming
 * later" tile uses a neutral icon instead of an approximation.
 */
const ICONS: Record<string, SimpleIcon> = {
  github: siGithub,
  discord: siDiscord,
  gmail: siGmail,
  notion: siNotion,
  googledrive: siGoogledrive,
  confluence: siConfluence,
};

export type ConnectorKey = "upload" | "github" | "discord" | "gmail" | "slack" | "notion" | "googledrive" | "confluence";

export function ConnectorIcon({ connector, className }: { connector: ConnectorKey | string; className?: string }) {
  if (connector === "upload") {
    return <Upload className={cn("text-brand-strong", className)} aria-hidden="true" />;
  }
  const icon = ICONS[connector];
  if (!icon) return <MessageSquare className={cn("text-fg-muted", className)} aria-hidden="true" />;
  return (
    <svg role="img" viewBox="0 0 24 24" className={className} fill={`#${icon.hex}`} aria-hidden="true">
      <path d={icon.path} />
    </svg>
  );
}

/** A logo in a bordered tile so mixed shapes line up (UI_DESIGN §2.6). */
export function LogoTile({ connector, size = 40 }: { connector: ConnectorKey | string; size?: number }) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center justify-center rounded-md border border-border",
        connector === "upload" ? "border-brand-border bg-brand-subtle" : "bg-bg",
      )}
      style={{ width: size, height: size }}
    >
      <ConnectorIcon connector={connector} className="size-[55%]" />
    </span>
  );
}

/** Placeholder mark until the owner supplies the real logo (UI_DESIGN §3.12). */
export function Logo({ withWordmark = true }: { withWordmark?: boolean }) {
  return (
    <span className="inline-flex items-center gap-2.5">
      <span className="inline-flex size-8 items-center justify-center rounded-md bg-brand-subtle text-[15px] font-semibold text-brand-strong">
        S
      </span>
      {withWordmark ? <span className="text-[15px] font-semibold tracking-[-0.02em] text-fg">Slate</span> : null}
    </span>
  );
}
