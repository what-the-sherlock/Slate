import { Suspense } from "react";
import { SearchView } from "./search-view";

export const metadata = { title: "Search" };

// SearchView keeps its state in the URL; useSearchParams needs a Suspense boundary.
export default function SearchPage() {
  return (
    <Suspense>
      <SearchView />
    </Suspense>
  );
}
