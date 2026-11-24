import { Search } from "lucide-react";
import Link from "next/link";
import { EmptyState } from "@/components/empty-state";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";

export const metadata = { title: "Search" };

export default function SearchPage() {
  return (
    <>
      <PageHeader title="Search" />
      <EmptyState
        icon={Search}
        title="Hybrid search is coming next"
        action={
          <Button asChild variant="secondary">
            <Link href="/sources">Add sources meanwhile</Link>
          </Button>
        }
      >
        Search arrives in milestone 2. Uploaded documents are already split, embedded and indexed.
      </EmptyState>
    </>
  );
}
