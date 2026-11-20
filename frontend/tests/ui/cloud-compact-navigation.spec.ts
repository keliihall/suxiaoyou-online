import { expect, test, type Locator, type Page } from "@playwright/test";
import { fullWorkbenchFixture } from "./fixtures/cloud-workbench";
import { cloudFixture, login } from "./fixtures/cloud-account";
import { auditVisibleContrast } from "./helpers/visible-contrast.mjs";

test.skip(process.env.NEXT_PUBLIC_CLOUD_MODE !== "true", "Cloud preferences belong to the authenticated user.");

const sidebar = (page: Page) => page.getByRole("complementary", { name: "Chat sidebar", exact: true });
const sidebarToggle = (page: Page) => sidebar(page).getByRole("button", { name: /^(Toggle sidebar|Collapse sidebar|Open sidebar|切换侧边栏|打开侧边栏)$/ });

async function compactWidth(element: Locator, expected: number) {
  await expect(element).toBeVisible();
  await expect.poll(async () => Math.round((await element.boundingBox())?.width ?? 0)).toBe(expected);
}

async function noOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
}

test("default icon rail follows the requested order and uses four matching primary controls", async ({ page }) => {
  await fullWorkbenchFixture(page, { runtimeReady: true });
  await page.goto("/c/new");
  await compactWidth(sidebar(page), 64);
  const rail = sidebar(page);
  await expect(rail.locator('img[src*="favicon"]')).toBeVisible();
  const top = [
    rail.getByRole("button", { name: "Search chats", exact: true }),
    rail.getByRole("link", { name: "Workbench", exact: true }),
    rail.getByRole("link", { name: "New chat", exact: true }),
    rail.getByTestId("sidebar-primary-more"),
  ];
  const bottom = [
    rail.getByRole("link", { name: "Settings", exact: true }),
    rail.getByTestId("sidebar-account-more"),
    rail.getByRole("button", { name: "退出登录", exact: true }),
    sidebarToggle(page),
  ];
  const positions: number[] = [];
  const styles: unknown[] = [];
  for (const control of [...top, ...bottom]) {
    await expect(control).toBeInViewport();
    const box = await control.boundingBox();
    expect(Math.round(box!.width)).toBe(44);
    expect(Math.round(box!.height)).toBe(44);
    positions.push(box!.y);
  }
  expect(positions).toEqual([...positions].sort((a, b) => a - b));
  for (const control of top) styles.push(await control.evaluate(element => {
    const style = getComputedStyle(element);
    return { color: style.color, background: style.backgroundColor, border: style.borderColor, radius: style.borderRadius };
  }));
  for (const style of styles) expect(style).toEqual(styles[0]);
  await rail.getByTestId("sidebar-primary-more").click();
  await expect(page.getByRole("menuitem", { name: "Plugins", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(rail.getByTestId("sidebar-primary-more")).toBeFocused();
});

test("legacy expanded preference migrates once and later manual expansion persists", async ({ page }) => {
  await fullWorkbenchFixture(page, { runtimeReady: true });
  await page.addInitScript(() => {
    const key = "suxiaoyou:cloud:cloud-user-a:suxiaoyou-sidebar";
    if (!localStorage.getItem("fixture-sidebar-migration-seeded")) {
      localStorage.setItem(key, JSON.stringify({ state: { isCollapsed: false, width: 260 }, version: 0 }));
      localStorage.setItem("fixture-sidebar-migration-seeded", "true");
    }
  });
  await page.goto("/c/new");
  await compactWidth(sidebar(page), 64);
  await sidebarToggle(page).click();
  await expect(sidebarToggle(page)).toHaveAttribute("aria-expanded", "true");
  await page.reload();
  await expect(sidebarToggle(page)).toHaveAttribute("aria-expanded", "true");
});

test("expanded workbench footer has one row ordered collapse, settings, More and logout", async ({ page }) => {
  await fullWorkbenchFixture(page, { runtimeReady: true });
  await page.goto("/c/new");
  const rail = sidebar(page);
  const toggle = rail.getByTestId("sidebar-collapse-toggle");
  await compactWidth(rail, 64);
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect.poll(async () => Math.round((await rail.boundingBox())?.width ?? 0)).toBe(300);
  await expect(toggle).toHaveCount(1);
  const controls = [
    toggle,
    rail.getByRole("link", { name: "Settings", exact: true }),
    rail.getByTestId("sidebar-account-more"),
    rail.getByRole("button", { name: "退出登录", exact: true }),
  ];
  const boxes = [];
  for (const control of controls) {
    await expect(control).toBeInViewport();
    const box = (await control.boundingBox())!;
    expect(Math.round(box.width)).toBe(44);
    expect(Math.round(box.height)).toBe(44);
    boxes.push(box);
  }
  expect(boxes.map(box => Math.round(box.y))).toEqual(Array(4).fill(Math.round(boxes[0].y)));
  expect(boxes.map(box => box.x)).toEqual(boxes.map(box => box.x).sort((a, b) => a - b));
  await toggle.focus();
  for (const control of controls.slice(1)) {
    await page.keyboard.press("Tab");
    await expect(control).toBeFocused();
  }
  await toggle.focus();
  await page.keyboard.press("Enter");
  await compactWidth(rail, 64);
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(toggle).toBeFocused();
  await expect(toggle).toHaveCount(1);
  await noOverflow(page);
});

test("home keeps suggestions and secondary actions inside one More menu", async ({ page }) => {
  const fixture = await fullWorkbenchFixture(page, { runtimeReady: true });
  await page.goto("/c/new");
  await expect(page.getByRole("heading", { name: "What would you like to do?", exact: true })).toBeVisible();
  const starters = page.locator('[data-testid^="starter-"]');
  await expect(starters).toHaveCount(0);
  await expect(page.getByTestId("workspace-select")).toHaveCount(0);
  const more = page.getByTestId("composer-more");
  await expect(more.locator("svg")).toHaveCount(1);
  await more.click();
  await expect(starters).toHaveCount(3);
  for (const starter of await starters.all()) {
    await expect(starter).toHaveAccessibleName(/.+/);
    await expect(starter).toBeVisible();
    const box = await starter.boundingBox();
    expect(box?.height).toBeGreaterThanOrEqual(44);
  }
  await starters.first().focus();
  await page.keyboard.press("Enter");
  await expect(starters).toHaveCount(0);
  const input = page.getByPlaceholder(/Describe the result you want/i);
  await expect(input).not.toHaveValue("");
  const send = page.getByRole("button", { name: "Send message", exact: true });
  await expect(send.locator("svg")).toHaveCount(1);
  await expect(send).toBeEnabled();
  await noOverflow(page);
  expect(fixture.engine.promptBodies).toHaveLength(0);
});

test("More and multi-agent panels preserve the input focus after an outside click", async ({ page }) => {
  const fixture = await fullWorkbenchFixture(page, { runtimeReady: true });
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/c/new");
  const more = page.getByTestId("composer-more");
  const input = page.getByPlaceholder(/Describe the result you want/i);
  const menu = page.getByRole("dialog", { name: "More actions", exact: true });
  const batch = page.getByRole("dialog", { name: "Multi-agent tasks", exact: true });
  const clickInputOutsidePanel = async () => {
    const box = await input.boundingBox();
    expect(box).not.toBeNull();
    await input.click({ position: { x: 8, y: 8 } });
    await expect(input).toBeFocused();
  };
  await more.click();
  await expect(menu).toBeVisible();
  await clickInputOutsidePanel();
  await expect(menu).toBeHidden();
  await page.keyboard.type("Keep typing after one click");
  await expect(input).toHaveValue("Keep typing after one click");
  await more.click();
  await menu.getByRole("button", { name: "Multi-agent tasks", exact: true }).click();
  await expect(batch).toBeVisible();
  await clickInputOutsidePanel();
  await expect(batch).toBeHidden();
  await more.click();
  await menu.getByRole("button", { name: "Multi-agent tasks", exact: true }).click();
  await expect(batch).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(batch).toBeHidden();
  await expect(more).toBeFocused();
  expect(fixture.engine.promptBodies).toHaveLength(0);
});

test("desktop sidebar folds with keyboard, preserves the draft and remembers the choice", async ({ page }) => {
  const fixture = await fullWorkbenchFixture(page, { runtimeReady: true });
  await page.goto("/c/new");
  const input = page.getByPlaceholder(/Describe the result you want/i);
  await input.fill("Keep this unsent draft while folding navigation");
  await compactWidth(sidebar(page), 64);
  await sidebarToggle(page).click();
  await expect(sidebarToggle(page)).toHaveAttribute("aria-expanded", "true");
  await sidebarToggle(page).focus();
  await page.keyboard.press("Enter");
  await compactWidth(sidebar(page), 64);
  await expect(sidebarToggle(page)).toHaveAttribute("aria-expanded", "false");
  await expect(sidebarToggle(page)).toBeFocused();
  await expect(input).toHaveValue("Keep this unsent draft while folding navigation");
  await expect(sidebar(page).getByTestId("sidebar-primary-more")).toBeVisible();
  await expect.poll(async () => Math.round((await page.locator("#main-content").boundingBox())?.x ?? 0)).toBe(64);
  await noOverflow(page);
  await page.reload();
  await compactWidth(sidebar(page), 64);
  await expect(sidebarToggle(page)).toHaveAttribute("aria-expanded", "false");
  await sidebarToggle(page).click();
  await expect(sidebarToggle(page)).toHaveAttribute("aria-expanded", "true");
  await expect(sidebar(page).getByRole("option", { name: /Quarterly planning notes/ })).toBeVisible();
  expect(fixture.engine.promptBodies).toHaveLength(0);
});

test("More preserves mode selection, workspace picking and multi-agent setup on a narrow screen", async ({ page }, info) => {
  const fixture = await fullWorkbenchFixture(page, { runtimeReady: true });
  await page.route("**/api/cloud-runtime/files/directories", route => route.fulfill({ json: {
    path: "/tenant/workspaces/project-a", parent: "/tenant/workspaces", dirs: [],
  } }));
  await page.setViewportSize({ width: 320, height: 568 });
  await page.goto("/c/new");
  const more = page.getByTestId("composer-more");
  const menu = page.getByRole("dialog", { name: "More actions", exact: true });
  await more.click();
  await menu.getByRole("button", { name: "Ask first", exact: true }).click();
  await expect(menu).toBeHidden();
  await expect(more).toBeFocused();
  await more.click();
  await expect(menu.getByRole("button", { name: "Ask first", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(menu.getByRole("button", { name: "上传文件夹", exact: true })).toBeVisible();
  await expect(menu.getByRole("button", { name: "Clear", exact: true })).toBeVisible();
  for (const mode of ["light", "dark"]) {
    await page.addStyleTag({ content: "*,*::before,*::after{transition:none!important;animation:none!important}" });
    await page.evaluate(mode => {
      document.documentElement.classList.remove("light", "dark");
      document.documentElement.classList.add(mode);
      document.documentElement.style.colorScheme = mode;
    }, mode);
    const audit = await page.evaluate(auditVisibleContrast, {});
    await info.attach(`overflow-menu-${mode}`, { body: JSON.stringify(audit, null, 2), contentType: "application/json" });
    expect(audit.failures).toEqual([]);
    expect((await menu.boundingBox())?.x).toBeGreaterThanOrEqual(0);
    await noOverflow(page);
  }
  await menu.getByTestId("workspace-select").click();
  const directory = page.getByRole("dialog", { name: "Set workspace", exact: true });
  await expect(directory).toBeVisible();
  await expect(menu).toBeHidden();
  await page.keyboard.press("Escape");
  await expect(directory).toBeHidden();
  await expect(more).toBeFocused();
  await more.click();
  await menu.getByRole("button", { name: "Multi-agent tasks", exact: true }).click();
  const batch = page.getByRole("dialog", { name: "Multi-agent tasks", exact: true });
  await expect(batch).toBeVisible();
  await expect(menu).toBeHidden();
  expect((await batch.boundingBox())?.width).toBeLessThanOrEqual(320);
  await page.keyboard.press("Escape");
  await expect(batch).toBeHidden();
  await expect(more).toBeFocused();
  expect(fixture.engine.promptBodies).toHaveLength(0);
  expect(fixture.calls.some(call => call.path === "/api/chat/respond")).toBe(false);
});

test("collapsed navigation keeps routes, search, new chat and account controls reachable", async ({ page }) => {
  const fixture = await fullWorkbenchFixture(page, { runtimeReady: true });
  await page.goto("/c/new");
  await compactWidth(sidebar(page), 64);
  const search = sidebar(page).getByRole("button", { name: "Search chats", exact: true });
  await search.click();
  const searchDialog = page.getByRole("dialog", { name: "Search chats", exact: true });
  await expect(searchDialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(searchDialog).toBeHidden();
  await expect(search).toBeFocused();
  await sidebar(page).getByTestId("sidebar-primary-more").click();
  await page.getByRole("menuitem", { name: "Plugins", exact: true }).click();
  await expect(page).toHaveURL(/\/plugins$/);
  await compactWidth(sidebar(page), 64);
  await sidebar(page).getByTestId("sidebar-primary-more").click();
  await page.getByRole("menuitem", { name: "Automations", exact: true }).click();
  await expect(page).toHaveURL(/\/automations$/);
  await sidebar(page).getByRole("link", { name: "New chat", exact: true }).click();
  await expect(page).toHaveURL(/\/c\/new$/);
  await sidebar(page).getByTestId("sidebar-primary-more").click();
  await expect(page.getByRole("menuitem", { name: "Add new project", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(sidebar(page).getByRole("link", { name: "Settings", exact: true })).toBeVisible();
  await sidebar(page).getByTestId("sidebar-account-more").click();
  await expect(page.getByRole("menuitem", { name: "云端管理", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await noOverflow(page);
  expect(fixture.engine.promptBodies).toHaveLength(0);
});

test("desktop collapse preference stays isolated between cloud users", async ({ page }) => {
  const fixture = await fullWorkbenchFixture(page, { runtimeReady: true });
  await page.goto("/c/new");
  await compactWidth(sidebar(page), 64);
  await sidebarToggle(page).click();
  await expect(sidebarToggle(page)).toHaveAttribute("aria-expanded", "true");
  const preference = "suxiaoyou:cloud:cloud-user-a:suxiaoyou-sidebar";
  expect(await page.evaluate(key => JSON.parse(localStorage.getItem(key)!).state.isCollapsed, preference)).toBe(false);
  let userId = "cloud-user-b";
  await page.route("**/api/cloud/auth/me", route => route.fulfill({ json: {
    user: { id: userId, email: `${userId}@example.test`, display_name: userId, role: "admin", is_active: true },
    csrf_token: "memory-only-csrf",
  } }));
  await page.reload();
  await compactWidth(sidebar(page), 64);
  await expect(sidebarToggle(page)).toHaveAttribute("aria-expanded", "false");
  expect(await page.evaluate(key => JSON.parse(localStorage.getItem(key)!).state.isCollapsed, preference)).toBe(false);
  userId = "cloud-user-a";
  await page.reload();
  await expect(sidebarToggle(page)).toHaveAttribute("aria-expanded", "true");
  await page.reload();
  await expect(sidebarToggle(page)).toHaveAttribute("aria-expanded", "true");
  expect(fixture.engine.promptBodies).toHaveLength(0);
});

test("settings keeps the collapsed rail and returns to the original conversation", async ({ page }) => {
  const fixture = await fullWorkbenchFixture(page, { runtimeReady: true });
  await page.goto("/c/_?sessionId=session-beta");
  await compactWidth(sidebar(page), 64);
  await sidebar(page).getByRole("link", { name: "Settings", exact: true }).click();
  const settings = page.getByRole("complementary", { name: "Settings sidebar", exact: true });
  await compactWidth(settings, 64);
  await expect(page.getByTestId("settings-back-to-app")).toHaveAccessibleName("Back to app");
  await settings.getByRole("button", { name: "Usage", exact: true }).click();
  await expect(page).toHaveURL(/\/settings\?tab=usage$/);
  await compactWidth(settings, 64);
  await noOverflow(page);
  await page.getByTestId("settings-back-to-app").click();
  await expect(page).toHaveURL(/\/c\/_\?sessionId=session-beta$/);
  await compactWidth(sidebar(page), 64);
  expect(fixture.engine.promptBodies).toHaveLength(0);
});

test("mobile drawer stays expanded independently of the desktop icon rail", async ({ page }) => {
  const fixture = await fullWorkbenchFixture(page, { runtimeReady: true });
  await page.goto("/c/new");
  await compactWidth(sidebar(page), 64);
  await page.setViewportSize({ width: 320, height: 568 });
  await page.getByRole("button", { name: "Toggle sidebar", exact: true }).click();
  const drawer = page.getByRole("dialog", { name: "Navigation", exact: true });
  await expect(drawer).toBeVisible();
  await expect(drawer.getByRole("button", { name: "New chat", exact: true })).toBeInViewport();
  await drawer.getByRole("link", { name: "Plugins", exact: true }).click();
  await expect(drawer).toBeHidden();
  await expect(page).toHaveURL(/\/plugins$/);
  await noOverflow(page);
  await page.setViewportSize({ width: 1440, height: 960 });
  await compactWidth(sidebar(page), 64);
  await expect(sidebarToggle(page)).toHaveAttribute("aria-expanded", "false");
  expect(fixture.engine.promptBodies).toHaveLength(0);
});

test("account navigation folds, restores after reload and still opens the full mobile drawer", async ({ page }) => {
  const fixture = await cloudFixture(page, true);
  await login(page);
  const accountSidebar = page.getByRole("complementary", { name: "账户导航", exact: true });
  await page.getByRole("button", { name: "收起导航", exact: true }).click();
  await compactWidth(accountSidebar, 68);
  await page.getByRole("button", { name: "个人模型", exact: true }).click();
  await expect(page.getByRole("heading", { name: "个人模型", exact: true })).toBeVisible();
  await page.reload();
  await compactWidth(accountSidebar, 68);
  await page.setViewportSize({ width: 390, height: 844 });
  const menu = page.getByRole("button", { name: "展开导航", exact: true });
  await menu.click();
  await expect(accountSidebar.getByRole("button", { name: "个人模型", exact: true })).toBeInViewport();
  expect((await accountSidebar.boundingBox())?.width).toBeGreaterThan(200);
  await page.keyboard.press("Escape");
  await expect(menu).toBeFocused();
  await noOverflow(page);
  await page.setViewportSize({ width: 1440, height: 960 });
  await compactWidth(accountSidebar, 68);
  expect(fixture.calls.filter(call => call.method === "DELETE")).toHaveLength(0);
});

test("compact desktop and mobile controls remain readable in both themes", async ({ page }, info) => {
  await fullWorkbenchFixture(page, { runtimeReady: true });
  await page.goto("/c/new");
  await compactWidth(sidebar(page), 64);
  for (const width of [1440, 320]) {
    await page.setViewportSize({ width, height: width === 320 ? 568 : 960 });
    for (const mode of ["light", "dark"]) {
      await page.addStyleTag({ content: "*,*::before,*::after{transition:none!important;animation:none!important}" });
      await page.evaluate(mode => {
        document.documentElement.classList.remove("light", "dark");
        document.documentElement.classList.add(mode);
        document.documentElement.style.colorScheme = mode;
      }, mode);
      const audit = await page.evaluate(auditVisibleContrast, {});
      await info.attach(`compact-${width}-${mode}`, { body: JSON.stringify(audit, null, 2), contentType: "application/json" });
      expect(audit.checked).toBeGreaterThan(0);
      expect(audit.failures).toEqual([]);
      await noOverflow(page);
    }
  }
});
