import { Suspense } from "react";
import { IntegrationsView } from "./integrations-view";

export const metadata = { title: "Integrations" };

// The view reads ?connect=<id>; useSearchParams needs a Suspense boundary.
export default function IntegrationsPage() {
  return (
    <Suspense>
      <IntegrationsView />
    </Suspense>
  );
}
