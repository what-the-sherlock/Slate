import { Suspense } from "react";
import { RunsView } from "./runs-view";

export const metadata = { title: "Source" };

// The id is only known at request time, so read it inside Suspense and let the rest of
// the route prerender (Next 16 cacheComponents).
export default function SourcePage({ params }: PageProps<"/sources/[id]">) {
  return (
    <Suspense>
      <Runs params={params} />
    </Suspense>
  );
}

async function Runs({ params }: { params: PageProps<"/sources/[id]">["params"] }) {
  const { id } = await params;
  return <RunsView sourceId={id} />;
}
