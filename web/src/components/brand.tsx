import { cn } from "@/lib/utils";
import { MessageSquare, Upload } from "lucide-react";
import {
  siAirtable,
  siAsana,
  siBasecamp,
  siBitbucket,
  siBox,
  siClickup,
  siCoda,
  siConfluence,
  siDiscord,
  siDropbox,
  siFigma,
  siGithub,
  siGitlab,
  siGmail,
  siGooglecalendar,
  siGoogledocs,
  siGoogledrive,
  siGooglesheets,
  siHubspot,
  siJira,
  siLinear,
  siLoom,
  siMiro,
  siNotion,
  siPagerduty,
  siSentry,
  siStackoverflow,
  siTelegram,
  siTrello,
  siZendesk,
  siZoom,
  type SimpleIcon,
} from "simple-icons";

/**
 * Brand marks from Simple Icons (CC0), drawn in each brand's own colour, never recoloured
 * (UI_DESIGN §2.6). Trademarks belong to their owners and are used only to name the
 * integration. Not available, so never approximated: Slack (withdrawn at Slack's request) and
 * Microsoft's marks. Too pale to read on a white tile, so left out: GitBook, Intercom.
 */
export const ICONS = {
  github: siGithub,
  gitlab: siGitlab,
  bitbucket: siBitbucket,
  jira: siJira,
  linear: siLinear,
  confluence: siConfluence,
  notion: siNotion,
  discord: siDiscord,
  telegram: siTelegram,
  stackoverflow: siStackoverflow,
  zoom: siZoom,
  loom: siLoom,
  gmail: siGmail,
  googledrive: siGoogledrive,
  googledocs: siGoogledocs,
  googlesheets: siGooglesheets,
  googlecalendar: siGooglecalendar,
  dropbox: siDropbox,
  box: siBox,
  figma: siFigma,
  miro: siMiro,
  asana: siAsana,
  trello: siTrello,
  clickup: siClickup,
  hubspot: siHubspot,
  zendesk: siZendesk,
  basecamp: siBasecamp,
  pagerduty: siPagerduty,
  sentry: siSentry,
  airtable: siAirtable,
  coda: siCoda,
} satisfies Record<string, SimpleIcon>;

export type ConnectorKey = "upload" | "slack" | keyof typeof ICONS;

// Backend connector ids that share another connector's mark (ADR-0028: email sources).
const ALIASES: Record<string, string> = {
  imap: "gmail",
  gmail_mbox: "gmail",
  google_calendar: "googlecalendar",
  google_drive: "googledrive",
};
const NAMES: Record<string, string> = {
  upload: "Files",
  imap: "Email",
  gmail_mbox: "Email import",
  gmail: "Gmail",
  google_calendar: "Google Calendar",
  google_drive: "Google Drive",
  slack: "Slack",
};

// Official full-colour marks (Wikimedia Commons, checked for scripts and external links),
// served from /public: simple-icons has only one-colour versions, and no Slack at all.
const COLOR_LOGOS = new Set(["gmail", "googledrive", "googlecalendar", "slack"]);

export function connectorName(connector: string): string {
  return NAMES[connector] ?? (ICONS as Record<string, SimpleIcon>)[connector]?.title ?? connector;
}

export function ConnectorIcon({ connector, className }: { connector: ConnectorKey | string; className?: string }) {
  if (connector === "upload") {
    return <Upload className={cn("text-brand-strong", className)} aria-hidden="true" />;
  }
  const key = ALIASES[connector] ?? connector;
  if (COLOR_LOGOS.has(key)) {
    // eslint-disable-next-line @next/next/no-img-element -- a tiny static SVG; next/image adds nothing
    return <img src={`/brand/logos/${key}.svg`} alt="" className={cn("object-contain", className)} aria-hidden="true" />;
  }
  const icon = (ICONS as Record<string, SimpleIcon>)[key];
  if (!icon) return <MessageSquare className={cn("text-fg-muted", className)} aria-hidden="true" />;
  return (
    <svg role="img" viewBox="0 0 24 24" className={className} fill={`#${icon.hex}`} aria-hidden="true">
      <path d={icon.path} />
    </svg>
  );
}

/** A logo in a bordered tile so mixed shapes line up (UI_DESIGN §2.6). */
export function LogoTile({ connector, size = 40, className }: { connector: ConnectorKey | string; size?: number; className?: string }) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center justify-center rounded-md border border-border",
        connector === "upload" ? "border-brand-border bg-brand-subtle" : "bg-bg",
        className,
      )}
      style={{ width: size, height: size }}
    >
      <ConnectorIcon connector={connector} className="size-[55%]" />
    </span>
  );
}

/**
 * The Slate mark, traced by hand from the owner's PNG (1254 px, frame 524 × 432) so it stays
 * crisp at any size. One black cut (with its two notch lines) splits a solid frame, drawn with
 * the even-odd rule. Inherits colour through currentColor.
 */
export function LogoMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 524 432" className={className} fill="currentColor" aria-hidden="true">
      <path
        fillRule="evenodd"
        d="M0 0H524V432H0Z M281 21H382V275H526V302H368L253 411H138V163H-2V136H158Z"
      />
    </svg>
  );
}

/** The logo: the white mark on a blue rounded tile, plus the wordmark (UI_DESIGN §3.12). */
export function Logo({ withWordmark = true, size = 24 }: { withWordmark?: boolean; size?: 24 | 28 }) {
  return (
    <span className="inline-flex items-center gap-2">
      <span
        className="inline-flex shrink-0 items-center justify-center rounded-md bg-brand text-white shadow-xs"
        style={{ width: size, height: size }}
      >
        <LogoMark className="w-[62%]" />
      </span>
      {withWordmark ? (
        <span className={cn("font-semibold tracking-[-0.02em] text-fg", size === 28 ? "text-[17px]" : "text-ui")}>Slate</span>
      ) : null}
    </span>
  );
}
