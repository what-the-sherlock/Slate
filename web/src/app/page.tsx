"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { Spinner } from "@/components/ui/button";
import { useMe, useSetupStatus } from "@/lib/api/hooks";

/** Entry: first run -> setup; signed out -> sign in; otherwise -> the app. */
export default function Home() {
  const router = useRouter();
  const status = useSetupStatus();
  const me = useMe();

  useEffect(() => {
    if (status.data && !status.data.set_up) router.replace("/setup");
    else if (me.data === null) router.replace("/sign-in");
    else if (me.data) router.replace("/sources");
  }, [status.data, me.data, router]);

  return (
    <main id="main" className="flex flex-1 items-center justify-center" aria-busy="true">
      <Spinner className="size-5 text-fg-subtle" />
      <span className="sr-only">Loading</span>
    </main>
  );
}
