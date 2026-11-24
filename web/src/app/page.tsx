import { StatusPanel } from "./status-panel";

export const metadata = { title: "Status" };

// M0 skeleton: proves web -> /api rewrite -> FastAPI -> Neon works end to end.
// The real UI follows docs/UI_DESIGN.md from M1 on.
export default function StatusPage() {
  return (
    <main className="mx-auto w-full max-w-xl px-4 py-16">
      <h1 className="text-[22px] leading-7 font-medium tracking-[-0.03em] text-fg">Slate</h1>
      <p className="mt-1 mb-6 text-fg-subtle">System status</p>
      <StatusPanel />
    </main>
  );
}
