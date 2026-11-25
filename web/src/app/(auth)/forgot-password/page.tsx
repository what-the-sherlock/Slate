import Link from "next/link";
import { Suspense } from "react";
import { AuthFrame } from "../auth-frame";
import { ForgotPasswordForm } from "./forgot-password-form";

export const metadata = { title: "Reset your password" };

export default function ForgotPasswordPage() {
  return (
    <AuthFrame
      headline="Reset your password."
      subtitle="We'll email you a 6-digit code. Resetting signs you out everywhere else."
      aside={
        <p className="text-body text-fg-muted">
          <span className="hidden sm:inline">Remembered it? </span>
          <Link href="/sign-in" className="font-semibold text-fg hover:text-brand">
            Sign in
          </Link>
        </p>
      }
    >
      {/* useSearchParams needs a Suspense boundary when the page is prerendered. */}
      <Suspense fallback={<div className="h-[58px] rounded-2xl bg-bg" />}>
        <ForgotPasswordForm />
      </Suspense>
    </AuthFrame>
  );
}
