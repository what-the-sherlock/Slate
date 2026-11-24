import { MessageSquare } from "lucide-react";
import Link from "next/link";
import { EmptyState } from "@/components/empty-state";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";

export const metadata = { title: "Chat" };

export default function ChatPage() {
  return (
    <>
      <PageHeader title="Chat" />
      <EmptyState
        icon={MessageSquare}
        title="Cited answers are coming next"
        action={
          <Button asChild variant="secondary">
            <Link href="/sources">Add sources meanwhile</Link>
          </Button>
        }
      >
        Chat arrives in milestone 3. Everything you upload now will be ready to ask about.
      </EmptyState>
    </>
  );
}
