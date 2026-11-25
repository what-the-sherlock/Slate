import { AuthFrame } from "../auth-frame";
import { VerifyForm } from "./verify-form";

export const metadata = { title: "Verify your email" };

export default function VerifyPage() {
  return (
    <AuthFrame
      headline="Check your inbox."
      subtitle="We sent a 6-digit code to your email. Enter it to finish setting up your workspace."
    >
      <VerifyForm />
    </AuthFrame>
  );
}
