import { Suspense } from "react";
import { ChatView } from "./chat-view";

export const metadata = { title: "Chat" };

// ChatView keeps the open chat in the URL (?c=); useSearchParams needs a Suspense boundary.
export default function ChatPage() {
  return (
    <Suspense>
      <ChatView />
    </Suspense>
  );
}
