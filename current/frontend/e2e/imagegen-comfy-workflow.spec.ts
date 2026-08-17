import { expect, test } from "@playwright/test";

/**
 * Evidence shot for #89: the ComfyUI custom-workflow textarea in Settings →
 * Image generation. Fixture mode (no backend); we click ComfyUI to reveal the
 * contextual fields, fill the checkpoint + a sample API-format graph, and capture.
 */
test("Settings → Image generation: ComfyUI custom-workflow textarea", async ({ page }, testInfo) => {
  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: /^settings$/i })).toBeVisible();

  const section = page.locator("#image-generation section");
  await expect(section).toBeVisible();
  await section.scrollIntoViewIfNeeded();

  await section.getByText("Configure image generation", { exact: true }).click();
  const comfy = section.getByRole("button", { name: /Self-hosted \(ComfyUI\)/ });
  await expect(comfy).toBeVisible();
  await comfy.click();

  // Base URL + checkpoint + the new custom-workflow textarea.
  await section.getByPlaceholder(/required for ComfyUI/i).fill("http://192.168.1.50:8188");
  await section.getByPlaceholder(/sd_xl_base_1\.0\.safetensors/i).fill("Illustrious-XL-v1.0.safetensors");
  const ta = section.getByPlaceholder(/Save \(API Format\)/i);
  await expect(ta).toBeVisible();
  await ta.fill(
    '{\n' +
      '  "1": { "class_type": "CheckpointLoaderSimple", "inputs": { "ckpt_name": "%ckpt%" } },\n' +
      '  "2": { "class_type": "CLIPTextEncode", "inputs": { "text": "%prompt%", "clip": ["1", 1] } },\n' +
      '  "5": { "class_type": "KSampler", "inputs": { "seed": %seed%, "model": ["1", 0] } }\n' +
      '}',
  );

  await section.scrollIntoViewIfNeeded();
  await page.screenshot({
    path: testInfo.outputPath("comfy-workflow-textarea.png"),
    fullPage: true,
  });
});
