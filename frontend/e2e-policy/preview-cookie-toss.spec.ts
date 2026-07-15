import { expect, test } from "@playwright/test";

const PARENT = "https://preview-cookie.test";
const CHILD =
  "https://p3s-deadbeef-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-8000.preview-cookie.test";

test("a generated child can send parent-domain duplicates that auth must not trust", async ({
  context,
  page,
}) => {
  await context.addCookies([
    {
      name: "disco_session",
      value: "valid-hostonly-session-shape",
      url: PARENT,
      httpOnly: true,
      secure: true,
      sameSite: "Strict",
    },
  ]);
  let parentCookieHeader = "";
  await page.route("https://**", async (route) => {
    if (new URL(route.request().url()).origin === PARENT) {
      parentCookieHeader = route.request().headers().cookie ?? "";
    }
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/html" },
      body: "<!doctype html><title>cookie policy probe</title>",
    });
  });

  await page.goto(CHILD);
  await page.evaluate(() => {
    document.cookie =
      "disco_path_preview_isolated=1; Domain=preview-cookie.test; Path=/; Secure; SameSite=Strict";
    document.cookie =
      "disco_session=child-domain-invalid; Domain=preview-cookie.test; Path=/; Secure; SameSite=Strict";
  });
  await page.goto(PARENT);

  expect(parentCookieHeader).toContain("disco_path_preview_isolated=1");
  expect(parentCookieHeader).toContain(
    "disco_session=valid-hostonly-session-shape",
  );
  expect(parentCookieHeader).toContain("disco_session=child-domain-invalid");
});
