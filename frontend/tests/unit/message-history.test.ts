import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test, { type TestContext } from "node:test";
import { QueryClient, QueryObserver } from "@tanstack/react-query";
import {
  fetchPreviousMessagePage,
  messageHistoryQueryOptions,
  refreshMessageHistory,
  type FetchMessagePage,
  type MessageHistory,
} from "../../src/lib/message-history.ts";
import type { MessageResponse, PaginatedMessages } from "../../src/types/message.ts";

function message(index: number, session = "session") : MessageResponse {
  return {
    id: `${session}-${index}`, session_id: session, time_created: new Date(index).toISOString(),
    data: { role: index % 2 === 0 ? "user" : "assistant" },
    parts: [{ id: `part-${index}`, message_id: `${session}-${index}`, session_id: session,
      time_created: new Date(index).toISOString(), data: { type: "text", text: `Message ${index}` } }],
  };
}

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => { resolve = done; });
  return { promise, resolve };
}

function fixture(t: TestContext, session = "session") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  const key = ["messages", session];
  let rows = Array.from({ length: 150 }, (_, index) => message(index, session));
  let onRead: (offset: number, signal: AbortSignal, page: PaginatedMessages) => Promise<void> | void = () => {};
  const calls: { offset: number; limit: number; signal: AbortSignal }[] = [];
  const fetchPage: FetchMessagePage = async (limit, requestedOffset, signal) => {
    const offset = requestedOffset < 0 ? Math.max(0, rows.length - limit) : requestedOffset;
    const page = { total: rows.length, offset, messages: rows.slice(offset, offset + limit) };
    calls.push({ offset: requestedOffset, limit, signal });
    await onRead(requestedOffset, signal, page);
    return page;
  };
  const options = () => messageHistoryQueryOptions(client, key, fetchPage);
  const observer = new QueryObserver(client, { ...options(), enabled: false });
  const extraObservers: { destroy: () => void }[] = [];
  t.after(() => { observer.destroy(); extraObservers.forEach((item) => item.destroy()); client.clear(); });
  const cache = () => client.getQueryData<MessageHistory>(key)!;
  const ids = () => cache().pages.flatMap((page) => page.messages.map((item) => item.id));
  const refresh = () => observer.refetch({ throwOnError: true });
  const previous = (signal = new AbortController().signal) => fetchPreviousMessagePage(client, key, fetchPage, signal);
  const seed = async (pages: number) => {
    await refresh();
    for (let index = 1; index < pages; index += 1) await previous();
  };
  const assertRange = (first: number, last: number) => {
    assert.deepEqual(ids(), Array.from({ length: last - first + 1 }, (_, index) => `${session}-${first + index}`));
    assert.equal(cache().pageParams.at(-1), -1);
    assert.equal(new Set(ids()).size, ids().length);
  };
  return { client, key, observer, options, cache, ids, calls, fetchPage, seed, refresh, previous, assertRange, extraObservers,
    setRows: (next: MessageResponse[]) => { rows = next; },
    rows: () => rows,
    onRead: (callback: typeof onRead) => { onRead = callback; } };
}

for (const pages of [1, 2, 3]) {
  test(`${pages} loaded pages survive repeated ordinary-query refetches with their latest anchor`, async (t) => {
    const f = fixture(t);
    await f.seed(pages);
    const first = 150 - pages * 50;
    f.assertRange(first, 149);
    await f.refresh();
    await f.refresh();
    f.assertRange(first, 149);
  });
}

test("an active DONE-style invalidation preserves all loaded history and includes new messages", async (t) => {
  const f = fixture(t);
  await f.seed(3);
  f.observer.setOptions({ ...f.options(), enabled: true, staleTime: Infinity });
  const unsubscribe = f.observer.subscribe(() => {});
  t.after(unsubscribe);
  f.setRows([...f.rows(), ...Array.from({ length: 10 }, (_, index) => message(150 + index))]);
  await f.client.invalidateQueries({ queryKey: f.key });
  f.assertRange(0, 159);
  assert.equal(f.cache().pages.at(-1)?.total, 160);
});

test("stale reentry and focus refresh use the same range-preserving query contract", async (t) => {
  const f = fixture(t);
  await f.seed(2);
  f.client.setQueryData(f.key, f.cache(), { updatedAt: Date.now() - 6_000 });
  const remount = new QueryObserver(f.client, { ...f.options(), staleTime: 5_000, refetchOnWindowFocus: true });
  f.extraObservers.push(remount);
  const unsubscribe = remount.subscribe(() => {});
  t.after(unsubscribe);
  await remount.refetch({ throwOnError: true });
  f.assertRange(50, 149);
  f.setRows([...f.rows(), message(150)]);
  f.client.setQueryData(f.key, f.cache(), { updatedAt: Date.now() - 6_000 });
  f.client.getQueryCache().find({ queryKey: f.key })!.onFocus();
  await remount.refetch({ throwOnError: true, cancelRefetch: false });
  f.assertRange(50, 150);
});

test("shared stats and sidebar prefetch observers cannot restore infinite-query truncation", async (t) => {
  const f = fixture(t);
  await f.client.prefetchQuery({ ...f.options(), staleTime: 60_000 });
  await f.previous();
  const stats = new QueryObserver(f.client, { ...f.options(), enabled: false, staleTime: 60_000 });
  f.extraObservers.push(stats);
  await stats.refetch({ throwOnError: true });
  f.assertRange(50, 149);
  for (const file of ["src/hooks/use-messages.ts", "src/hooks/use-message-stats.ts", "src/components/layout/session-item.tsx"]) {
    const source = readFileSync(file, "utf8");
    assert.match(source, /messageHistoryQueryOptions/);
    assert.doesNotMatch(source, /useInfiniteQuery|prefetchInfiniteQuery/);
  }
});

test("legacy truncated cache is repaired instead of preserving an old numeric last-page cursor", async (t) => {
  const f = fixture(t);
  f.client.setQueryData<MessageHistory>(f.key, {
    pages: [{ total: 150, offset: 50, messages: f.rows().slice(50, 100) }], pageParams: [50],
  });
  await f.refresh();
  f.assertRange(50, 149);
});

test("appending more than one page fills every gap between loaded history and the fresh latest page", async (t) => {
  const f = fixture(t);
  await f.seed(2);
  f.setRows([...f.rows(), ...Array.from({ length: 120 }, (_, index) => message(150 + index))]);
  await f.refresh();
  f.assertRange(50, 269);
});

test("legal edit shrink atomically removes deleted messages and reanchors a formerly out-of-range cache", async (t) => {
  const f = fixture(t);
  await f.seed(2);
  f.setRows(f.rows().slice(0, 21));
  await f.refresh();
  f.assertRange(0, 20);
  assert.equal(f.cache().pages.at(-1)?.total, 21);
});

test("removing old rows shifts surviving loaded messages without submitting an empty stale-offset page", async (t) => {
  const f = fixture(t);
  await f.seed(2);
  f.setRows(f.rows().slice(100));
  await f.refresh();
  f.assertRange(100, 149);
  assert.equal(f.cache().pages[0].offset, 0);
});

test("a smaller count during older-page reads retries from latest before committing any partial window", async (t) => {
  const f = fixture(t);
  await f.seed(3);
  let shrunk = false;
  f.onRead((offset) => {
    if (offset === -1 && !shrunk) { shrunk = true; f.setRows(f.rows().slice(0, 31)); }
  });
  await f.refresh();
  f.assertRange(0, 30);
});

test("new messages arriving before an older load preserve both its requested range and the newest tail", async (t) => {
  const f = fixture(t);
  await f.seed(1);
  f.setRows([...f.rows(), ...Array.from({ length: 10 }, (_, index) => message(150 + index))]);
  await f.previous();
  f.assertRange(50, 159);
});

test("a late older response cannot restore messages removed by an authoritative edit", async (t) => {
  const f = fixture(t);
  await f.seed(1);
  const entered = deferred(), release = deferred();
  f.onRead(async (offset) => { if (offset === 50) { entered.resolve(); await release.promise; } });
  const pending = f.previous();
  await entered.promise;
  f.client.setQueryData<MessageHistory>(f.key, {
    pages: [{ total: 5, offset: 0, messages: f.rows().slice(0, 5) }], pageParams: [-1],
  });
  release.resolve();
  await pending;
  f.assertRange(0, 4);
});

test("a late refetch cannot overwrite an authoritative edit installed during the request", async (t) => {
  const f = fixture(t);
  await f.seed(1);
  const entered = deferred(), release = deferred();
  f.onRead(async (offset) => { if (offset === -1) { entered.resolve(); await release.promise; } });
  const pending = f.refresh();
  await entered.promise;
  f.client.setQueryData<MessageHistory>(f.key, {
    pages: [{ total: 5, offset: 0, messages: f.rows().slice(0, 5) }], pageParams: [-1],
  });
  release.resolve();
  await pending;
  f.assertRange(0, 4);
});

test("a poll racing an older prepend refreshes the expanded range and includes the appended tail", async (t) => {
  const f = fixture(t);
  await f.seed(1);
  const olderEntered = deferred(), releaseOlder = deferred(), pollEntered = deferred(), releasePoll = deferred();
  let delayPoll = true;
  f.onRead(async (offset) => {
    if (offset === 50) { olderEntered.resolve(); await releaseOlder.promise; }
    if (offset === -1 && delayPoll) { delayPoll = false; pollEntered.resolve(); await releasePoll.promise; }
  });
  const older = f.previous();
  await olderEntered.promise;
  f.setRows([...f.rows(), ...Array.from({ length: 10 }, (_, index) => message(150 + index))]);
  const poll = f.refresh();
  await pollEntered.promise;
  releaseOlder.resolve();
  await older;
  releasePoll.resolve();
  await poll;
  f.assertRange(50, 159);
});

test("a completed latest refresh wins over a stale older-page response", async (t) => {
  const f = fixture(t);
  await f.seed(1);
  const entered = deferred(), release = deferred();
  f.onRead(async (offset) => { if (offset === 50) { entered.resolve(); await release.promise; } });
  const older = f.previous();
  await entered.promise;
  f.setRows([...f.rows(), message(150)]);
  await f.refresh();
  release.resolve();
  await older;
  f.assertRange(100, 150);
});

test("older loading cancels an in-flight refresh and ignores its late response", async (t) => {
  const f = fixture(t);
  await f.seed(1);
  const entered = deferred(), release = deferred();
  let delayed = true;
  f.onRead(async (offset) => {
    if (offset === -1 && delayed) { delayed = false; entered.resolve(); await release.promise; }
  });
  const poll = f.refresh();
  await entered.promise;
  await f.previous();
  assert.equal(f.calls.findLast((call) => call.offset === -1)?.signal.aborted, true);
  release.resolve();
  await poll;
  f.assertRange(50, 149);
});

test("aborted older loads do not append even if the transport returns after cancellation", async (t) => {
  const f = fixture(t);
  await f.seed(1);
  const entered = deferred(), release = deferred();
  f.onRead(async (offset) => { if (offset === 50) { entered.resolve(); await release.promise; } });
  const controller = new AbortController();
  const pending = f.previous(controller.signal);
  await entered.promise;
  controller.abort();
  release.resolve();
  await assert.rejects(pending, { name: "AbortError" });
  f.assertRange(100, 149);
});

test("cross-session late responses remain isolated by the shared query key", async (t) => {
  const f = fixture(t);
  const otherKey = ["messages", "other"];
  const entered = deferred(), release = deferred();
  f.onRead(async () => { entered.resolve(); await release.promise; });
  const pending = f.refresh();
  await entered.promise;
  f.client.setQueryData<MessageHistory>(otherKey, {
    pages: [{ total: 1, offset: 0, messages: [message(0, "other")] }], pageParams: [-1],
  });
  release.resolve();
  await pending;
  assert.deepEqual(f.client.getQueryData<MessageHistory>(otherKey)?.pages[0].messages.map((item) => item.id), ["other-0"]);
  f.assertRange(100, 149);
});

test("failed range refresh leaves the previous complete cache intact", async (t) => {
  const f = fixture(t);
  await f.seed(3);
  const before = f.cache();
  f.onRead((offset) => { if (offset === 50) throw new Error("read failed"); });
  await assert.rejects(f.refresh(), /read failed/);
  assert.equal(f.cache(), before);
  f.assertRange(0, 149);
});

test("an empty conversation retains a latest anchor and aborted refreshes perform no read", async () => {
  const controller = new AbortController();
  const empty = await refreshMessageHistory(undefined,
    async () => ({ total: 0, offset: 0, messages: [] }), controller.signal);
  assert.deepEqual(empty, { pages: [{ total: 0, offset: 0, messages: [] }], pageParams: [-1] });
  controller.abort();
  await assert.rejects(refreshMessageHistory(undefined, async () => {
    throw new Error("must not read");
  }, controller.signal), { name: "AbortError" });
});
