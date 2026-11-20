import { expect, test } from "@playwright/test";
import {
  appendLongConversationTurn,
  mock苏小有Api,
  seed苏小有Storage,
} from "./fixtures/suxiaoyou-api";

// Deterministic browser regression for cache/DOM behavior. The separate
// operator acceptance executes a genuine configured DeepSeek task on release.
test("keeps loaded history and new results across repeated real-time polls", async ({ page }) => {
  test.setTimeout(90_000);
  await seed苏小有Storage(page);
  const state = await mock苏小有Api(page);
  for (let i = 0; i < 20; i += 1) appendLongConversationTurn(state, `Extra prompt ${i}`, `Extra result ${i}`);
  await page.route("**/api/sessions/session-long/todos", (route) => route.fulfill({
    status: 200, contentType: "application/json", body: JSON.stringify({ todos: [] }),
  }));
  let latestReads = 0;
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/messages/session-long" && url.searchParams.get("offset") === "-1") latestReads += 1;
  });
  await page.goto("/c/session-long");
  await expect(page.getByText("Long assistant turn 080:", { exact: false })).toHaveCount(1);
  const scroller = page.getByTestId("message-list-scroller");
  await scroller.evaluate((element) => { element.scrollTop = 0; });
  await expect.poll(() => scroller.locator("[data-message-id]").count()).toBeGreaterThanOrEqual(50);
  const beforeIds = await scroller.locator("[data-message-id]").evaluateAll((elements) => elements.map((element) => element.getAttribute("data-message-id")));
  const readsBefore = latestReads;
  await expect.poll(() => latestReads, { timeout: 32_000, intervals: [500] }).toBeGreaterThanOrEqual(readsBefore + 2);
  const afterIds = await scroller.locator("[data-message-id]").evaluateAll((elements) => elements.map((element) => element.getAttribute("data-message-id")));
  expect(beforeIds.every((id) => afterIds.includes(id))).toBe(true);
  await expect(page.getByText("Long assistant turn 080:", { exact: false })).toHaveCount(1);

  appendLongConversationTurn(state, "A genuinely new tail in the fixture", "New result stays after polling");
  await expect(page.getByText("Long assistant turn 081:", { exact: false })).toHaveCount(1, { timeout: 15_000 });
  const withNewIds = await scroller.locator("[data-message-id]").evaluateAll((elements) => elements.map((element) => element.getAttribute("data-message-id")));
  expect(beforeIds.every((id) => withNewIds.includes(id))).toBe(true);
});
