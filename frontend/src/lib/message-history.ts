import type { InfiniteData, QueryClient, QueryKey } from "@tanstack/react-query";
import type { PaginatedMessages } from "../types/message.ts";

export const MESSAGE_PAGE_SIZE = 50;
export type MessageHistory = InfiniteData<PaginatedMessages, number>;
export type FetchMessagePage = (
  limit: number,
  offset: number,
  signal: AbortSignal,
) => Promise<PaginatedMessages>;

function checkAbort(signal: AbortSignal) {
  if (signal.aborted) {
    throw signal.reason ?? new DOMException("Aborted", "AbortError");
  }
}

function completePage(page: PaginatedMessages, offset: number, limit: number) {
  return Number.isSafeInteger(page.total) && page.total >= 0
    && page.offset === offset
    && page.messages.length === Math.min(limit, Math.max(0, page.total - offset));
}

/** Refresh the loaded range from a fresh latest-page anchor, then install it atomically. */
export async function refreshMessageHistory(
  previous: MessageHistory | undefined,
  fetchPage: FetchMessagePage,
  signal: AbortSignal,
  expandToOffset?: number,
): Promise<MessageHistory> {
  for (let attempt = 0; attempt < 3; attempt += 1) {
    checkAbort(signal);
    const latest = await fetchPage(MESSAGE_PAGE_SIZE, -1, signal);
    checkAbort(signal);
    if (!completePage(latest, Math.max(0, latest.total - MESSAGE_PAGE_SIZE), MESSAGE_PAGE_SIZE)) {
      continue;
    }

    const loaded = previous?.pages.filter((page) => page.messages.length > 0) ?? [];
    const previousTotal = previous?.pages.at(-1)?.total ?? latest.total;
    // A deletion can move surviving loaded messages towards the start. Fetch
    // conservatively from that earlier bound; an out-of-range old offset must
    // never replace a nonempty latest page with an empty page.
    const start = loaded.length
      ? Math.min(latest.offset, Math.max(0,
        Math.min(...loaded.map((page) => page.offset), expandToOffset ?? Infinity)
          + Math.min(0, latest.total - previousTotal),
      ))
      : latest.offset;
    const requests: { offset: number; limit: number }[] = [];
    for (let offset = start; offset < latest.offset; offset += MESSAGE_PAGE_SIZE) {
      requests.push({ offset, limit: Math.min(MESSAGE_PAGE_SIZE, latest.offset - offset) });
    }
    const pages: PaginatedMessages[] = [];
    // Bound parallel reads when a user has loaded a long conversation.
    for (let index = 0; index < requests.length; index += 4) {
      const batch = requests.slice(index, index + 4);
      pages.push(...await Promise.all(batch.map(({ limit, offset }) => fetchPage(limit, offset, signal))));
      checkAbort(signal);
    }
    if (pages.some((page, index) => page.total < latest.total
      || !completePage(page, requests[index].offset, requests[index].limit))) {
      continue;
    }
    return {
      pages: [...pages.map((page) => ({ ...page, total: latest.total })), latest],
      pageParams: [...requests.map(({ offset }) => offset), -1],
    };
  }
  throw new Error("Message history changed during refresh");
}

function isOlderPageExtension(previous: MessageHistory | undefined, current: MessageHistory) {
  if (!previous || current.pages.length <= previous.pages.length) return false;
  const extra = current.pages.length - previous.pages.length;
  return previous.pages.every((page, index) => page === current.pages[extra + index]
    && previous.pageParams[index] === current.pageParams[extra + index]);
}

/** Every subscriber/prefetch for the shared key uses this ordinary-query contract. */
export function messageHistoryQueryOptions(
  queryClient: QueryClient,
  queryKey: QueryKey,
  fetchPage: FetchMessagePage,
  expandToOffset?: number,
) {
  return {
    queryKey,
    queryFn: async ({ signal }: { signal: AbortSignal }): Promise<MessageHistory> => {
      for (let attempt = 0; attempt < 3; attempt += 1) {
        const previous = queryClient.getQueryData<MessageHistory>(queryKey);
        const refreshed = await refreshMessageHistory(previous, fetchPage, signal, expandToOffset);
        checkAbort(signal);
        const current = queryClient.getQueryData<MessageHistory>(queryKey);
        if (current === previous) return refreshed;
        // An edit or another authoritative cache replacement wins over an
        // older in-flight response. A concurrent prepend is safe to refresh
        // again, preserving both the older range and any newly appended tail.
        if (!current || !isOlderPageExtension(previous, current)) return current ?? refreshed;
      }
      return queryClient.getQueryData<MessageHistory>(queryKey)!;
    },
  };
}

/** Prepend one older page without allowing a stale response to revive edited history. */
export async function fetchPreviousMessagePage(
  queryClient: QueryClient,
  queryKey: QueryKey,
  fetchPage: FetchMessagePage,
  signal: AbortSignal,
): Promise<void> {
  checkAbort(signal);
  await queryClient.cancelQueries({ queryKey, exact: true });
  checkAbort(signal);
  const previous = queryClient.getQueryData<MessageHistory>(queryKey);
  const first = previous?.pages[0];
  if (!previous || !first || first.offset <= 0) return;
  const offset = Math.max(0, first.offset - MESSAGE_PAGE_SIZE);
  const limit = first.offset - offset;
  const page = await fetchPage(limit, offset, signal);
  checkAbort(signal);
  if (queryClient.getQueryData<MessageHistory>(queryKey) !== previous) return;
  if (page.total !== previous.pages.at(-1)?.total || !completePage(page, offset, limit)) {
    await queryClient.cancelQueries({ queryKey, exact: true });
    checkAbort(signal);
    if (queryClient.getQueryData<MessageHistory>(queryKey) !== previous) return;
    await queryClient.fetchQuery({
      ...messageHistoryQueryOptions(queryClient, queryKey, fetchPage, offset), staleTime: 0,
    });
    return;
  }
  queryClient.setQueryData<MessageHistory>(queryKey, {
    pages: [page, ...previous.pages],
    pageParams: [offset, ...previous.pageParams],
  });
}
