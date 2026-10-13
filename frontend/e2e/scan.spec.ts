import { test, expect } from "@playwright/test";
import * as path from "path";
import { fileURLToPath } from "url";

// The project is ESM ("type": "module"), so __dirname is undefined here — derive it.
const _dir = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.resolve(_dir, "../../tests/fixtures/e2e");

test("sign up → upload → review → save → my pulls", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /Pack Scanner/ })).toBeVisible();

  // Saving a pull needs a trainer: sign up through the login modal.
  const handle = `e2e_${Date.now().toString(36)}`;
  await page.locator(".topbar").getByRole("button", { name: /Log in/ }).click();
  const dialog = page.getByRole("dialog", { name: "Trainer login" });
  await dialog.getByRole("button", { name: /Sign up/ }).click();
  await dialog.getByLabel("Email").fill(`${handle}@example.com`);
  await dialog.getByLabel("Trainer handle").fill(handle);
  await dialog.getByLabel("Password").fill("correct-horse-battery");
  await dialog.getByRole("button", { name: "Sign up", exact: true }).click();
  await expect(page.locator(".trainer-chip")).toContainText(`@${handle}`);

  // Mode select → One photo.
  await page.getByRole("button", { name: /Press start/ }).click();
  await page.getByRole("button", { name: /One photo/ }).click();

  // Step 1: staircase via the upload fallback; step 2: code card.
  await expect(page.getByRole("heading", { name: "Snap your pack" })).toBeVisible();
  await page.locator('input[type="file"]').setInputFiles(path.join(FIXTURES, "staircase.jpg"));
  await expect(page.getByRole("heading", { name: "Code card" })).toBeVisible();
  await page.locator('input[type="file"]').setInputFiles(path.join(FIXTURES, "code.jpg"));

  // Review renders the real cards with DB names, and the code.
  await expect(page.getByText("Test Mon A")).toBeVisible({ timeout: 45_000 });
  await expect(page.getByText("Test Mon B")).toBeVisible();
  await expect(page.getByText("Test Mon C")).toBeVisible();
  await expect(page.getByText("TEST1-CODE2-CARD3")).toBeVisible();

  // The upload path is ungrided, so a phantom row can come back flagged; the
  // save button stays disabled until every flagged row is resolved.
  for (const btn of await page.getByRole("button", { name: "Keep anyway" }).all()) {
    await btn.click();
  }

  await page.getByRole("button", { name: /save pack/ }).click();
  await expect(page.getByRole("heading", { name: "Pack logged!" })).toBeVisible();
  await expect(page.getByText(/\d+ cards? saved to your pulls/)).toBeVisible();

  // The pull shows up under My Pulls.
  await page.getByRole("navigation", { name: "Main" }).getByRole("button", { name: /PULLS|My Pulls/ }).first().click();
  await expect(page.getByRole("heading", { name: "My pulls" })).toBeVisible();
  await expect(page.locator(".pull-card")).toHaveCount(1);
});
