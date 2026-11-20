import { expect, test, type Page, type Route } from "@playwright/test";
import { cloudFixture } from "./fixtures/cloud-account";
import { completeCaptcha } from "./fixtures/cloud-captcha";
import { auditVisibleContrast } from "./helpers/visible-contrast.mjs";

test.skip(process.env.NEXT_PUBLIC_CLOUD_MODE !== "true", "Login verification belongs to the cloud build.");

async function openVerification(page: Page, password = "cloud-password-123") {
  await page.getByLabel("邮箱", { exact: true }).fill("cloud@example.com");
  await page.getByLabel("密码", { exact: true }).fill(password);
  await page.getByRole("button", { name: "登录工作台", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "安全验证" })).toBeVisible();
}

test("login has a compact circular button and centered brand, validates fields before opening and automatically signs in after verification", async ({ page }) => {
  const fixture = await cloudFixture(page);
  await page.goto("/cloud?manage=1");
  const login = page.getByRole("button", { name: "登录工作台", exact: true });
  await expect(login).toBeEnabled();
  await expect(login).toHaveText("");
  await expect(page.getByRole("heading", { name: "欢迎回来" })).toHaveCount(0);
  await expect(page.locator(".cloud-login .cloud-brand-name")).toHaveText("苏小有");
  await expect(page.locator(".cloud-login .cloud-brand-name small")).toHaveCount(0);
  const logo = page.locator(".cloud-login .cloud-brand-symbol img");
  await expect(logo).toBeVisible();
  expect(await logo.evaluate(element => {
    const logo = element.getBoundingClientRect();
    const main = element.closest("main")!;
    const name = main.querySelector(".cloud-brand-name")!.getBoundingClientRect();
    const form = main.getBoundingClientRect();
    return Math.abs((logo.x + name.right) / 2 - form.x - form.width / 2);
  })).toBeLessThanOrEqual(1);
  expect(await login.evaluate(element => ({ width: element.getBoundingClientRect().width, height: element.getBoundingClientRect().height, radius: getComputedStyle(element).borderRadius }))).toEqual({ width: 40, height: 40, radius: "50%" });
  expect(await login.evaluate(element => {
    const button = element.getBoundingClientRect();
    const form = element.closest("form")!.getBoundingClientRect();
    return Math.abs(button.x + button.width / 2 - form.x - form.width / 2);
  })).toBeLessThanOrEqual(1);
  await expect(page.getByTestId("cloud-captcha")).toHaveCount(0);
  expect(fixture.captcha.state.issued).toBe(0);
  await login.click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.getByLabel("邮箱", { exact: true }).fill("invalid-email");
  await page.getByLabel("密码", { exact: true }).fill("cloud-password-123");
  await login.click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(fixture.captcha.state.issued).toBe(0);
  await page.getByLabel("邮箱", { exact: true }).fill("cloud@example.com");
  await page.getByLabel("密码", { exact: true }).press("Enter");
  await expect(page.getByRole("dialog", { name: "安全验证" })).toBeVisible();
  expect(fixture.calls.filter(call => call.path.endsWith("/auth/login"))).toHaveLength(0);
  await completeCaptcha(page);
  await expect(page.getByRole("heading", { name: "用量与额度", exact: true })).toBeVisible();
  expect(fixture.captcha.state.verified).toBe(1);
  expect(fixture.calls.filter(call => call.path.endsWith("/auth/login"))).toHaveLength(1);
});

test("a wrong puzzle attempt requires refresh before another attempt", async ({ page }) => {
  const fixture = await cloudFixture(page);
  await page.goto("/cloud?manage=1");
  await openVerification(page);
  const slider = page.getByRole("slider");
  await expect(slider).toBeEnabled();
  const initiallyIssued = fixture.captcha.state.issued;
  await slider.focus();
  await page.keyboard.press("End");
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("captcha-status")).toHaveText("验证未通过，请刷新重试");
  await expect(slider).toBeDisabled();
  expect(fixture.captcha.state.rejected).toBe(1);
  expect(fixture.calls.filter(call => call.path.endsWith("/auth/login"))).toHaveLength(0);
  await page.getByTestId("captcha-refresh").click();
  await completeCaptcha(page);
  await expect(page.getByRole("heading", { name: "用量与额度", exact: true })).toBeVisible();
  expect(fixture.captcha.state.issued).toBeGreaterThan(initiallyIssued);
});

test("a failed password closes verification and the next login opens a fresh puzzle", async ({ page }) => {
  const fixture = await cloudFixture(page);
  await page.route("**/api/cloud/auth/login", route => route.fulfill({ status: 401, json: { detail: { code: "invalid_credentials", message: "邮箱或密码错误" } } }));
  await page.goto("/cloud?manage=1");
  await openVerification(page, "wrong-password-123");
  await completeCaptcha(page);
  await expect(page.getByRole("alert").filter({ hasText: "邮箱或密码不正确" })).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const initiallyIssued = fixture.captcha.state.issued;
  await expect(page.getByLabel("邮箱", { exact: true })).toHaveValue("cloud@example.com");
  await expect(page.getByRole("button", { name: "登录工作台", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "登录工作台", exact: true }).click();
  await expect(page.getByRole("slider")).toBeEnabled();
  expect(fixture.captcha.state.issued).toBeGreaterThan(initiallyIssued);
});

test("expired verification stays in the dialog and refresh recovers", async ({ page }) => {
  const fixture = await cloudFixture(page);
  fixture.captcha.state.expiresIn = 2;
  await page.goto("/cloud?manage=1");
  await openVerification(page);
  await expect(page.getByRole("slider")).toBeEnabled();
  await expect(page.getByTestId("captcha-status")).toHaveText("验证已过期，请刷新");
  await expect(page.getByRole("slider")).toBeDisabled();
  fixture.captcha.state.expiresIn = 120;
  await page.getByTestId("captcha-refresh").click();
  await completeCaptcha(page);
  await expect(page.getByRole("heading", { name: "用量与额度", exact: true })).toBeVisible();
});

test("cancelling verification restores focus and ignores a late proof", async ({ page }) => {
  const fixture = await cloudFixture(page);
  let pending: Route | undefined;
  await page.route("**/api/cloud/auth/captcha/verify", route => { pending = route; });
  await page.goto("/cloud?manage=1");
  await openVerification(page);
  const slider = page.getByRole("slider");
  await expect(slider).toBeEnabled();
  await slider.focus();
  await page.keyboard.press("End");
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("cloud-captcha")).toHaveAttribute("data-phase", "verifying");
  await expect.poll(() => Boolean(pending)).toBe(true);
  await page.getByRole("button", { name: "关闭验证" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "登录工作台", exact: true })).toBeFocused();
  await pending!.fulfill({ json: { proof: "late-cancelled-proof", expires_in: 120 } }).catch(() => {});
  expect(fixture.calls.filter(call => call.path.endsWith("/auth/login"))).toHaveLength(0);
  await page.unroute("**/api/cloud/auth/captcha/verify");
  const initiallyIssued = fixture.captcha.state.issued;
  await page.getByRole("button", { name: "登录工作台", exact: true }).click();
  await expect(page.getByRole("slider")).toBeEnabled();
  expect(fixture.captcha.state.issued).toBeGreaterThan(initiallyIssued);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "登录工作台", exact: true })).toBeFocused();
});

test("a stalled image request times out and refresh recovers", async ({ page }) => {
  await cloudFixture(page);
  let stall = true;
  await page.route("**/api/cloud/auth/captcha/challenge", route => stall ? undefined : route.fallback());
  await page.clock.install();
  await page.goto("/cloud?manage=1");
  await openVerification(page);
  await expect(page.getByTestId("cloud-captcha")).toHaveAttribute("data-phase", "loading");
  await page.clock.fastForward(10_100);
  await expect(page.getByTestId("captcha-status")).toHaveText("连接超时，请刷新");
  stall = false;
  await page.getByTestId("captcha-refresh").click();
  await completeCaptcha(page);
  await expect(page.getByRole("heading", { name: "用量与额度", exact: true })).toBeVisible();
});

test("a stalled login times out, closes the dialog and restores the login form", async ({ page }) => {
  const fixture = await cloudFixture(page);
  await page.route("**/api/cloud/auth/login", () => undefined);
  await page.clock.install();
  await page.goto("/cloud?manage=1");
  await openVerification(page);
  await completeCaptcha(page);
  await expect(page.getByRole("dialog", { name: "安全验证" })).toHaveAttribute("aria-busy", "true");
  await expect(page.getByTestId("captcha-status")).toHaveText("正在登录…");
  const initiallyIssued = fixture.captcha.state.issued;
  await page.clock.fastForward(10_100);
  await expect(page.getByRole("alert").filter({ hasText: /超时/ })).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByLabel("邮箱", { exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "登录工作台", exact: true }).click();
  await expect(page.getByRole("slider")).toBeEnabled();
  expect(fixture.captcha.state.issued).toBeGreaterThan(initiallyIssued);
});

test("minimal login and dialog fit 320px in both themes and dragging automatically signs in", async ({ page }, info) => {
  const fixture = await cloudFixture(page);
  await page.setViewportSize({ width: 320, height: 568 });
  await page.goto("/cloud?manage=1");
  await page.addStyleTag({ content: "*,*::before,*::after{transition:none!important;animation:none!important}" });
  for (const phase of ["form", "dialog"]) {
    if (phase === "dialog") await openVerification(page);
    for (const mode of ["light", "dark"]) {
      await page.evaluate(mode => {
        document.documentElement.classList.remove("light", "dark");
        document.documentElement.classList.add(mode);
        document.documentElement.style.colorScheme = mode;
      }, mode);
      const result = await page.evaluate(auditVisibleContrast, {});
      await info.attach(`minimal-login-${phase}-${mode}`, { body: JSON.stringify(result), contentType: "application/json" });
      expect(result.failures).toEqual([]);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    }
  }
  const dialogBounds = (await page.getByRole("dialog", { name: "安全验证" }).boundingBox())!;
  expect(dialogBounds.x).toBeGreaterThanOrEqual(0);
  expect(dialogBounds.x + dialogBounds.width).toBeLessThanOrEqual(320);
  const closeBounds = (await page.getByRole("button", { name: "关闭验证" }).boundingBox())!;
  expect(closeBounds.width).toBeGreaterThanOrEqual(44);
  expect(closeBounds.height).toBeGreaterThanOrEqual(44);
  const slider = page.getByRole("slider");
  await expect(slider).toBeEnabled();
  await slider.scrollIntoViewIfNeeded();
  const bounds = (await slider.boundingBox())!;
  expect(bounds.height).toBeGreaterThanOrEqual(44);
  const thumb = 44;
  const x = bounds.x + thumb / 2;
  const y = bounds.y + bounds.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + (bounds.width - thumb) * 144 / 272, y, { steps: 15 });
  await page.mouse.up();
  await expect(page.getByRole("heading", { name: "用量与额度", exact: true })).toBeVisible();
  expect(fixture.captcha.state.verified).toBe(1);
});
