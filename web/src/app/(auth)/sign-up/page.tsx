import Link from "next/link";
import { AuthFrame } from "../auth-frame";
import { SignUpForm } from "./sign-up-form";

export const metadata = { title: "Create your account" };

export default function SignUpPage() {
  return (
    <AuthFrame
      headline="Ask your own documents."
      subtitle="Create a free account in three quick steps. You get a private workspace: upload files, connect sources and ask questions with cited answers."
      aside={
        <p className="text-body text-fg-muted">
          <span className="hidden sm:inline">Already have an account? </span>
          <Link href="/sign-in" className="font-semibold text-fg hover:text-brand">
            Sign in
          </Link>
        </p>
      }
    >
      <SignUpForm />
    </AuthFrame>
  );
}
