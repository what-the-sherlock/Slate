"use client";

import { cn } from "@/lib/utils";
import { useState } from "react";
import { connectorName, LogoTile, type ConnectorKey } from "@/components/brand";

// Two rows that scroll in opposite directions. The list is rendered twice and the track moves
// by -50%, so the loop has no visible seam (globals.css .sl-marquee).
const ROW_A: ConnectorKey[] = [
  "upload", "github", "gitlab", "bitbucket", "jira", "linear", "confluence", "notion",
  "discord", "telegram", "stackoverflow", "zoom", "loom",
];
const ROW_B: ConnectorKey[] = [
  "gmail", "googledrive", "googledocs", "googlesheets", "googlecalendar", "dropbox", "box",
  "figma", "miro", "asana", "trello", "clickup", "hubspot", "zendesk", "basecamp",
  "pagerduty", "sentry", "airtable", "coda",
];
export const MARQUEE_CONNECTORS = [...ROW_A, ...ROW_B];

/** The word that rolls in the tagline, in order. */
const ROLL: ConnectorKey[] = ["github", "gmail", "notion", "jira", "discord", "googledrive", "figma", "linear", "zendesk", "upload"];

export function ConnectMarquee({ ms, paused }: { ms: number; paused: boolean }) {
  const [hovered, setHovered] = useState<ConnectorKey | null>(null);
  const rolled = ROLL[ms < 0 ? 0 : Math.floor(ms / 1_500) % ROLL.length] ?? "github";
  const word = connectorName(hovered ?? rolled);
  return (
    <div className="flex flex-col gap-3">
      <div
        className="sl-marquee flex flex-col gap-2"
        data-paused={paused || hovered !== null || undefined}
        onMouseLeave={() => setHovered(null)}
      >
        <Row items={ROW_A} onHover={setHovered} />
        <Row items={ROW_B} onHover={setHovered} reverse />
      </div>
      <div>
        <p className="flex h-6 items-baseline gap-1.5 overflow-hidden text-ui font-semibold tracking-[-0.01em] text-fg">
          One search box for
          <span key={word} className="inline-block animate-in text-brand duration-300 ease-out fade-in slide-in-from-bottom-3">
            {word === "Files" ? "your files" : word}
          </span>
        </p>
        <p className="text-caption text-fg-subtle">{MARQUEE_CONNECTORS.length} tools and counting</p>
      </div>
    </div>
  );
}

function Row({ items, reverse = false, onHover }: { items: ConnectorKey[]; reverse?: boolean; onHover: (k: ConnectorKey) => void }) {
  return (
    <div className="overflow-hidden py-0.5">
      <div className={cn("sl-marquee-track flex w-max flex-nowrap gap-2 pr-2", reverse && "sl-marquee-reverse")}>
        {[...items, ...items].map((key, i) => (
          <span
            key={`${key}-${i}`}
            onMouseEnter={() => onHover(key)}
            className="shrink-0 transition-transform duration-200 hover:-translate-y-0.5"
          >
            <LogoTile connector={key} size={30} className="shadow-xs" />
          </span>
        ))}
      </div>
    </div>
  );
}
