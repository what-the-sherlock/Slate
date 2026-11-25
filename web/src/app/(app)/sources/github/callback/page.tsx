import { Suspense } from "react";
import { GitHubCallback } from "./github-callback";

export const metadata = { title: "Connect GitHub" };

// Reads GitHub's redirect from the URL; useSearchParams needs a Suspense boundary.
export default function GitHubCallbackPage() {
  return (
    <Suspense>
      <GitHubCallback />
    </Suspense>
  );
}
