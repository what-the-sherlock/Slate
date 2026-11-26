import type { QueryClient } from "@tanstack/react-query";
import { chatStore } from "@/lib/chat-store";

/**
 * Everything this tab holds belongs to one workspace: cached queries *and* chats kept in
 * memory (with answers maybe still streaming). Call before showing another workspace.
 */
export function resetWorkspaceState(client: QueryClient): void {
  chatStore.reset();
  client.clear();
}
