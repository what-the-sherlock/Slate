import { Suspense } from "react";
import { DiscordCallback } from "./discord-callback";

export const metadata = { title: "Connect Discord" };

// Reads Discord's redirect from the URL; useSearchParams needs a Suspense boundary.
export default function DiscordCallbackPage() {
  return (
    <Suspense>
      <DiscordCallback />
    </Suspense>
  );
}
