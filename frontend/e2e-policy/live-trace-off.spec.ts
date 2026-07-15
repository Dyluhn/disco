import { writeFileSync } from "node:fs";

import { expect, test } from "@playwright/test";

test("an authenticated live failure does not retain a trace", async ({
  context,
  page,
}) => {
  await context.addCookies([
    {
      name: "disco_session",
      value: "fake-session-policy-probe",
      url: "http://trace-policy.invalid/",
      httpOnly: true,
      sameSite: "Strict",
    },
  ]);
  await page.setExtraHTTPHeaders({ "X-Disco-CSRF": "fake-csrf-policy-probe" });
  await page.route("http://trace-policy.invalid/**", async (route) => {
    const requestHeaders = route.request().headers();
    if (
      !requestHeaders.cookie?.includes(
        "disco_session=fake-session-policy-probe",
      )
    ) {
      throw new Error(
        "trace policy probe request omitted its fake session cookie",
      );
    }
    if (requestHeaders["x-disco-csrf"] !== "fake-csrf-policy-probe") {
      throw new Error(
        "trace policy probe request omitted its fake CSRF header",
      );
    }
    await route.fulfill({
      status: 200,
      headers: {
        "content-type": "text/html",
        "x-disco-csrf": "fake-csrf-policy-probe",
      },
      body: "<!doctype html><title>trace policy</title><main>authenticated fake</main>",
    });
  });
  await page.goto("http://trace-policy.invalid/authenticated");
  await expect(page.locator("main")).toHaveText("authenticated fake");

  const marker = process.env.DISCO_TRACE_POLICY_MARKER;
  if (!marker) throw new Error("DISCO_TRACE_POLICY_MARKER is required");
  writeFileSync(marker, "browser reached intentional failure\n", {
    encoding: "utf8",
  });

  // The wrapper requires this exact test to fail after authenticated browser
  // traffic, then proves the configured artifact directory has no trace.zip.
  expect("policy-probe").toBe("intentional-failure");
});
