import { Suspense } from "react";
import { AuthFrame } from "../auth-frame";
import { SignInForm } from "./sign-in-form";

export const metadata = { title: "Sign in" };

export default function SignInPage() {
  return (
    <AuthFrame
      headline={
        <>
          Ask your knowledge.
          <br />
          Get answers you can check.
        </>
      }
      subtitle="Search documents, code, chat and email, with every answer cited to the passage it came from."
    >
      {/* useSearchParams needs a Suspense boundary when the page is prerendered. */}
      <Suspense fallback={<div className="h-[58px] rounded-2xl bg-bg" />}>
        <SignInForm />
      </Suspense>
    </AuthFrame>
  );
}
