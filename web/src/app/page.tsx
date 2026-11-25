"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { Spinner } from "@/components/ui/button";
import { useMe } from "@/lib/api/hooks";

/** Entry: signed out -> sign in; unverified -> verify; otherwise -> the app (ADR-0021). */
export default function Home() {
  const router = useRouter();
  const me = useMe();

  useEffect(() => {
    if (me.data === null) router.replace("/sign-in");
    else if (me.data && !me.data.email_verified) router.replace("/verify");
    else if (me.data) router.replace("/home");
  }, [me.data, router]);

  return (
    <main id="main" className="flex flex-1 items-center justify-center" aria-busy="true">
      <Spinner className="size-5 text-fg-subtle" />
      <span className="sr-only">Loading</span>
    </main>
  );
}
