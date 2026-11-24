import { AuthFrame } from "../auth-frame";
import { SetupForm } from "./setup-form";

export const metadata = { title: "Set up" };

export default function SetupPage() {
  return (
    <AuthFrame
      headline="Set up your workspace."
      subtitle="Create the owner account. You'll connect sources and invite people from inside Slate."
    >
      <SetupForm />
    </AuthFrame>
  );
}
