import { Suspense } from "react";
import { IntegrationsCallback } from "./integrations-callback";

export const metadata = { title: "Connecting" };

// Reads the provider's redirect from the URL; useSearchParams needs a Suspense boundary.
export default function IntegrationsCallbackPage() {
  return (
    <Suspense>
      <IntegrationsCallback />
    </Suspense>
  );
}
