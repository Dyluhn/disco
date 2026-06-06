import { firefox } from "playwright";
const b = await firefox.launch();
const page = await b.newPage({ viewport: { width: 1320, height: 1000 } });
async function theme(t){ await page.evaluate((x)=>{document.documentElement.classList.remove("dark","light");document.documentElement.classList.add(x);}, t); await page.evaluate(()=>document.fonts.ready); await page.waitForTimeout(200); }

// --- A: Settings sandbox section, both themes ---
await page.goto("http://localhost:5173/settings", { waitUntil: "networkidle" });
await page.getByRole("heading", { name: "Sandbox" }).waitFor({ timeout: 8000 });
await page.getByRole("radio", { name: /Local container sandbox backend/i }).waitFor();
const section = page.locator("section", { has: page.getByRole("heading", { name: "Sandbox" }) });
for (const t of ["dark","light"]){ await theme(t); await section.scrollIntoViewIfNeeded(); await section.screenshot({ path:`screenshots/settings-sandbox-${t}.png` }); console.log("✓ settings-sandbox-"+t); }
await theme("dark");

// --- B: live preview — drive a build task that serves a page ---
await page.goto("http://localhost:5173/", { waitUntil: "networkidle" });
await page.evaluate(()=>document.fonts.ready);
await page.getByRole("radio", { name: "build" }).click();
await page.getByPlaceholder(/describe what you want/i).fill(
  "Do two tool calls: 1) file_write index.html with '<h1 style=\"font-family:sans-serif\">Hello from the agent preview</h1>'. 2) shell: nohup python3 -m http.server 8000 >/dev/null 2>&1 & sleep 1 . Then stop.");
await page.keyboard.press("Enter");
// wait for the run to finish (the agent + container take time)
await page.getByText(/Finished/i).waitFor({ timeout: 180000 }).catch(()=>console.log("(no Finished marker)"));
await page.getByRole("tab", { name: /Preview/i }).click();
await page.waitForTimeout(6000); // let the preview poll + the iframe load
await theme("dark");
await page.screenshot({ path:"screenshots/build-preview-dark.png", fullPage:true });
console.log("✓ build-preview-dark");
await b.close();
