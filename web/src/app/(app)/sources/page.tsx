import { Suspense } from "react";
import { SourcesView } from "./sources-view";

export const metadata = { title: "Sources" };

// SourcesView reads ?add=1, and useSearchParams needs a Suspense boundary when prerendered.
export default function SourcesPage() {
  return (
    <Suspense>
      <SourcesView />
    </Suspense>
  );
}
