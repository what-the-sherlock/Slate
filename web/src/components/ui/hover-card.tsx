"use client";

import * as React from "react";
import { HoverCard as HoverCardPrimitive } from "radix-ui";
import { cn } from "@/lib/utils";

// shadcn's HoverCard over the radix-ui package we already ship (no new dependency).
// UI_DESIGN §3.4: opens after 300 ms hover or on focus, stays open while the pointer is
// inside, shadow-md, radius-lg, 360 px for citation previews.

function HoverCard({ openDelay = 300, closeDelay = 120, ...props }: React.ComponentProps<typeof HoverCardPrimitive.Root>) {
  return <HoverCardPrimitive.Root data-slot="hover-card" openDelay={openDelay} closeDelay={closeDelay} {...props} />;
}

function HoverCardTrigger(props: React.ComponentProps<typeof HoverCardPrimitive.Trigger>) {
  return <HoverCardPrimitive.Trigger data-slot="hover-card-trigger" {...props} />;
}

function HoverCardContent({ className, align = "start", sideOffset = 6, ...props }: React.ComponentProps<typeof HoverCardPrimitive.Content>) {
  return (
    <HoverCardPrimitive.Portal>
      <HoverCardPrimitive.Content
        data-slot="hover-card-content"
        align={align}
        sideOffset={sideOffset}
        className={cn(
          "z-50 w-[360px] max-w-[calc(100vw-2rem)] origin-(--radix-hover-card-content-transform-origin) rounded-lg border border-border bg-bg text-fg shadow-md outline-none",
          "data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95 duration-150",
          className,
        )}
        {...props}
      />
    </HoverCardPrimitive.Portal>
  );
}

export { HoverCard, HoverCardContent, HoverCardTrigger };
