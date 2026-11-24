import { Suspense } from "react";
import { AppShell } from "@/components/app-shell";
import { Spinner } from "@/components/ui/button";

// AppShell reads the pathname; under dynamic routes (/sources/[id]) that suspends during
// prerendering, so it needs a Suspense boundary (Next 16 cacheComponents).
export default function AppLayout({ children }: LayoutProps<"/">) {
  return (
    <Suspense
      fallback={
        <div className="flex flex-1 items-center justify-center" aria-busy="true">
          <Spinner className="size-5 text-fg-subtle" />
        </div>
      }
    >
      <AppShell>{children}</AppShell>
    </Suspense>
  );
}
