import Link from "next/link";
import { AuthFrame } from "../auth-frame";
import { SetupForm } from "./setup-form";

export const metadata = { title: "Set up" };

export default function SetupPage() {
  return (
    <AuthFrame
      headline="Set up your workspace."
      subtitle="Create the owner account in four quick steps. You'll connect sources and invite people from inside Slate."
      aside={
        <p className="text-body text-fg-muted">
          <span className="hidden sm:inline">Already set up? </span>
          <Link href="/sign-in" className="font-semibold text-fg hover:text-brand">
            Sign in
          </Link>
        </p>
      }
    >
      <SetupForm />
    </AuthFrame>
  );
}
