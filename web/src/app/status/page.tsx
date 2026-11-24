import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { Logo } from "@/components/brand";
import { PanelLabel } from "@/components/panel";
import { StatusPanel } from "./status-panel";

export const metadata = { title: "Status" };

// Public health page: proves web -> /api rewrite -> FastAPI -> Neon works end to end.
export default function StatusPage() {
  return (
    <div className="flex min-h-svh flex-col bg-subtle">
      <header className="flex h-14 items-center border-b border-border bg-bg px-4 sm:px-8">
        <div className="mx-auto flex w-full max-w-xl items-center justify-between">
          <Logo />
          <Link href="/" className="inline-flex items-center gap-1 text-caption text-fg-muted hover:text-fg">
            <ArrowLeft className="size-3.5" aria-hidden="true" />
            Back to Slate
          </Link>
        </div>
      </header>
      <main id="main" className="mx-auto w-full max-w-xl px-4 py-12">
        <PanelLabel>System status</PanelLabel>
        <h1 className="mt-1 mb-1 text-page font-semibold tracking-[-0.02em] text-fg">All the moving parts</h1>
        <p className="mb-6 text-body text-fg-subtle">Live checks of the API, the database and the schema version.</p>
        <StatusPanel />
      </main>
    </div>
  );
}
